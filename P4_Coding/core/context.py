"""Context —— 插件运行时：服务、effect、事件、动态依赖、插件生命周期、变更锁。

    服务槽位    provide / retract / get / require；命名单槽，重名与缺失报错
    effect      ctx.effect(name, install)：装载期的登记自动回卷
    事件        on / emit（广播）/ waterfall（中间件链）
    动态依赖    ctx.inject(deps, callback)：依赖齐了运行回调，依赖走了自动拆
    插件        load / unload / disable / enable；状态 pending / active / disabled / failed
    稳定化      依赖齐了自动激活、提供者走了自动退位；装载顺序无关
    变更       公共操作串行化（可重入）；有效变更使 revision + 1 并记录 ChangeReport

    回卷顺序：子 effect 先于父；同级后进先出；一个 disposer 抛错不打断其余，
    全部跑完后重抛第一个异常。
    装载失败：本次登记的一切被回卷，服务表还原。
    句柄：装载返回的卸载句柄认"这一次装载"，重复调用 / 已回卷 / 被同名新对象
    顶替都是空操作；按名字的 API（unload_effect / unload_plugin）严格报错。

    async-first：生命周期操作（provide / retract / effect / inject / load / unload /
    disable / enable / emit / waterfall）都是 awaitable；插件的 setup、effect 的
    install、disposer、事件 handler、inject callback 都可以是同步或异步函数，内核
    统一等待。**单事件循环模型**：一个 Context 只能在一个事件循环里用，非线程安全；
    只读属性是同步快照，在别的 task 的变更 await 期间可能读到中间态。锁内不要再
    spawn 一个 task 并等它操作同一个 Context（会死锁）。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from core.plugin import (
    Disposer,
    matches_plugin_requirement,
    plugin_injects,
    plugin_name,
    plugin_optionals,
    plugin_requires,
    plugin_setup,
)

#: 插件状态。
#:
#:     pending   已装载、依赖未齐（依赖来了会自动激活）
#:     active    运行中（它提供的服务可用）
#:     disabled  被停用：回卷了 effect，但登记还在，一键可恢复
#:     failed    激活时 setup 抛错（错误留在台账，供排查与重试）
PluginStatus = Literal["pending", "active", "disabled", "failed"]

#: 插件自己的 effect 名字前缀（`plugin:<插件名>`）。
#: 这个命名空间只由 Context 内部使用——用户 effect 不许占用（见 `effect`）。
PLUGIN_EFFECT_PREFIX = "plugin:"

#: 动态依赖回调的 effect 名字前缀（`inject:<序号>`）：同样是内部命名空间。
INJECT_EFFECT_PREFIX = "inject:"

#: effect 的装载函数：同步或异步；返回撤销函数、None，或它们的 awaitable。
Install = Callable[[], Any]

#: 事件处理器：同步或异步；广播（emit）收 `**payload`，瀑布收 `(value, **context)`。
EventHandler = Callable[..., Any]

#: `require` 用的哨兵：区分"键不存在"与"值恰好是 None"。
_MISSING = object()


async def _maybe_await(value: Any) -> Any:
    """同步值原样返回，awaitable 则等待（内核唯一的"异步兼容"机制）。"""
    if inspect.isawaitable(value):
        return await value
    return value


class _AsyncRLock:
    """可重入异步锁：同一 task 可重入，其他 task 排队。

    变更操作在 await 插件代码期间仍持锁；同一 task 里的嵌套公共操作（插件
    setup 里再 load_plugin）直接放行并并入最外层变更。注意：**不要**在持有锁的
    回调里 spawn 新 task 再等它操作同一个 Context（会死锁）。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def __aenter__(self) -> None:
        task = asyncio.current_task()
        if self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._depth = 1

    async def __aexit__(self, *_exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


class DuplicateSlotError(ValueError):
    """同名服务槽重复提供（fail loud：拒绝本次注册，原值原样保留）。"""


class MissingServiceError(KeyError):
    """取一个不存在的服务槽（fail loud：缺失是错误，不是"没有"）。"""


class DuplicatePluginError(ValueError):
    """同名插件重复装载（fail loud：一个运行时里一个插件名只留一份实例）。"""


class MissingPluginError(KeyError):
    """操作一个不在台账里的插件（fail loud：卸载 / 停用 / 启用都要求它在）。"""


class DuplicateEffectError(ValueError):
    """同名 effect 重复装载（fail loud：名字是回卷的地址，必须唯一）。"""


class MissingEffectError(KeyError):
    """卸载一个不存在的 effect（fail loud：防止拼写错误静默生效）。"""


@dataclass(frozen=True)
class PluginInfo:
    """一个插件的状态快照（观测用）。"""

    name: str
    status: PluginStatus
    inject: tuple[str, ...]
    missing: tuple[str, ...]  # 当前未满足的**必需**依赖（status=pending 时有意义）
    optional: tuple[str, ...]
    missing_optionals: tuple[str, ...]  # 可选依赖里当前取不到的那些（"降级中"）
    provided: tuple[str, ...]  # active 期间提供的服务（退位级联按它扩散）
    error: BaseException | None = None
    #: **插件级**依赖声明（`requires`）：这些插件名在场，本插件才完整。
    requires: tuple[str, ...] = ()
    #: 当前缺的插件级依赖（不阻断生命周期；由装配方在装配期检查）。
    missing_requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeReport:
    """一次有效变更的报告。

    retired / activated 是插件名，按发生顺序；同一次变更里先退位又激活的插件
    会同时出现在两边。动态依赖用 injections_started / injections_stopped 记录
    （值是 inject:<序号>）。
    """

    revision: int
    retired: tuple[str, ...]
    activated: tuple[str, ...]
    injections_started: tuple[str, ...] = ()
    injections_stopped: tuple[str, ...] = ()


#: 动态依赖回调的状态。
#:
#:     pending  依赖未齐（齐了自动运行回调）
#:     active   回调正在生效（它注册的一切挂在自己的 effect 下）
#:     failed   回调抛错（错误留着；依赖**变化**后会重试——不原地空转）
InjectionStatus = Literal["pending", "active", "failed"]


@dataclass(frozen=True)
class InjectionInfo:
    """一条动态依赖订阅的状态快照（观测用）。"""

    name: str  # `inject:<序号>`（effect 名，也是报告里的标识）
    deps: tuple[str, ...]
    status: InjectionStatus
    missing: tuple[str, ...]
    error: BaseException | None = None


@dataclass
class _InjectionRecord:
    """一条动态依赖订阅：依赖齐了就运行 callback（放进一个子 effect）。"""

    seq: int
    deps: tuple[str, ...]
    callback: Callable[[Context], Any]
    #: 注册时所在的 effect 名（装载期注册的订阅随那个 effect 一起回卷）；
    #: None = 顶层注册（调用方持有句柄负责卸载）。
    owner_effect: str | None = None
    status: InjectionStatus = "pending"
    effect: str | None = None  # 回调正在生效时的 effect 名
    error: BaseException | None = None


@dataclass
class _PluginRecord:
    """台账里的一条插件记录。"""

    name: str
    setup: Any  # SetupFn：Callable[[Context], Any]（同步/异步均可）
    inject: tuple[str, ...]
    optional: tuple[str, ...] = ()
    #: 插件级依赖（`requires`）：只做装配期校验，不参与激活（见 core/plugin.py）。
    requires: tuple[str, ...] = ()
    status: PluginStatus = "pending"
    effect: str | None = None  # 激活时创建的 effect 名（退位时回卷的就是它）
    provided: tuple[str, ...] = ()
    error: BaseException | None = None


@dataclass
class _Effect:
    """一个可回卷单元：名字 + 自己的撤销动作 + 子 effect + 父引用。"""

    name: str
    parent: _Effect | None = None
    disposers: list[Disposer] = field(default_factory=list)
    children: list[_Effect] = field(default_factory=list)


class Context:
    """插件容器与台账。

    装载方：

        ctx = Context()
        await ctx.load_plugin(Reporter())   # 依赖未齐则 pending，等依赖
        await ctx.load_plugin(Store())      # 依赖齐则自动激活
        await ctx.unload_plugin("store")    # 依赖它的插件自动退位
        await ctx.disable_plugin("store")   # 保留登记，enable_plugin 恢复

    插件作者：

        class MyPlugin:
            name = "my-plugin"
            inject = ("sessions",)      # 必需依赖：缺了先挂起
            optional = ("approval",)    # 可选依赖：缺了照常激活

            async def setup(self, ctx):
                await ctx.provide("greeting", "你好")    # 注册即回卷
                await ctx.effect("ticker", self._start)
    """

    def __init__(self) -> None:
        self._slots: dict[str, Any] = {}
        #: 每个槽的提供令牌：回卷时只撤"自己那一次提供"（槽可能已被 retract
        #: 后由别的 effect 重新提供，不能按名字盲删）。
        self._slot_tokens: dict[str, object] = {}
        #: 槽的提供者插件名（仅插件装载期的 provide 记账；用于准确级联与报错）。
        self._slot_owners: dict[str, str] = {}
        #: 正在激活的插件（provide 据此记 owner；settle 期间不会嵌套激活）。
        self._activating: _PluginRecord | None = None
        self._plugins: dict[str, _PluginRecord] = {}
        self._effects: dict[str, _Effect] = {}
        #: 正在装载的 effect 栈（父子关系与"注册即回卷"都靠它）。
        self._stack: list[_Effect] = []
        #: 事件订阅表：事件名 → [(令牌, handler)]；令牌让退订精确到"哪一次订阅"。
        self._handlers: dict[str, list[tuple[int, EventHandler]]] = {}
        self._handler_seq = 0
        #: 变更串行化：按 task 可重入的异步锁（单事件循环模型，非线程安全）。
        self._lock = _AsyncRLock()
        #: 变更嵌套深度 + 最外层变更开始时的状态签名（见 `_change`）。
        self._change_depth = 0
        self._change_before: tuple[Any, ...] | None = None
        self._revision = 0
        self._last_change: ChangeReport | None = None
        self._report_retired: list[str] = []
        self._report_activated: list[str] = []
        self._report_injections_started: list[str] = []
        self._report_injections_stopped: list[str] = []
        #: 动态依赖订阅（`ctx.inject`）：序号 → 记录。
        self._injections: dict[int, _InjectionRecord] = {}
        self._injection_seq = 0
        #: settle 重入保护（见 _settle）：稳定化过程中不再嵌套稳定化。
        self._settling = False
        #: 回卷深度：disposer 运行期间不推送稳定态（半拆世界里 settle 会看错）。
        self._unwinding = 0
        #: 退位级联期间被抑制的插件名：它们刚被停用，但提供者的服务还没撤——
        #: 不抑制的话，级联途中触发的 settle 会看到它们"依赖齐备"又立刻复活。
        self._suppressed: set[str] = set()

    # ------------------------------------------------------------------
    # 变更串行化 / revision
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        """世界版本号：每次有效变更 +1。"""
        return self._revision

    @property
    def last_change(self) -> ChangeReport | None:
        """最近一次有效变更的报告；还没变过则为 None。"""
        return self._last_change

    def _signature(self) -> tuple[Any, ...]:
        """当前世界签名：服务表、插件状态、effect 表、动态订阅状态。"""
        return (
            tuple(self._slots),
            tuple((record.name, record.status) for record in self._plugins.values()),
            tuple(self._effects),
            tuple((record.seq, record.status) for record in self._injections.values()),
        )

    @asynccontextmanager
    async def _change(self) -> AsyncIterator[None]:
        """把一次公共操作包成一次变更：加锁，结束时比较签名并更新 revision。

        嵌套调用并入最外层：一次外层动作只产生一个 revision。
        """
        async with self._lock:
            outermost = self._change_depth == 0
            if outermost:
                self._change_before = self._signature()
                self._report_retired = []
                self._report_activated = []
                self._report_injections_started = []
                self._report_injections_stopped = []
            self._change_depth += 1
            try:
                yield
            finally:
                self._change_depth -= 1
                if outermost:
                    before, self._change_before = self._change_before, None
                    if before != self._signature():
                        self._revision += 1
                        self._last_change = ChangeReport(
                            revision=self._revision,
                            retired=tuple(self._report_retired),
                            activated=tuple(self._report_activated),
                            injections_started=tuple(self._report_injections_started),
                            injections_stopped=tuple(self._report_injections_stopped),
                        )

    # ------------------------------------------------------------------
    # 服务（命名单槽）
    # ------------------------------------------------------------------

    async def provide(self, key: str, value: Any) -> None:
        """提供一项服务；同名已存在时抛 DuplicateSlotError，原值保留。

        装载期调用会自动登记回卷。提供后推进稳定态：等待这项服务的插件可能在
        这里被激活，其 setup 抛错会从本次调用抛出。

        不返回刚提供的值（便于把 provide 直接当作 effect 的 install 使用）。
        """
        if not isinstance(key, str) or not key:
            raise ValueError(f"服务名必须是非空字符串，收到：{key!r}")
        async with self._change():
            if key in self._slots:
                raise DuplicateSlotError(
                    f"服务已提供：{key}（重复注册直接报错，不静默覆盖；要换实现先 retract）"
                )
            token = object()
            self._slots[key] = value
            self._slot_tokens[key] = token
            if self._activating is not None:
                self._slot_owners[key] = self._activating.name
            if self._stack:  # 装载期：登记自动回卷（卸载时只撤这一次提供）
                self._stack[-1].disposers.append(
                    lambda k=key, t=token: self._drop_slot(k, t)
                )
            await self._settle()

    async def retract(self, key: str) -> Any:
        """撤销一项服务并返回原值；不存在时抛 MissingServiceError。

        依赖它的活跃插件随之退位（深的先退，清理异常收齐后抛第一个），退回
        pending。撤销本身不会因 effect 回卷而恢复；装载失败时整表还原是例外。
        """
        async with self._change():
            if key not in self._slots:
                raise MissingServiceError(f"服务不存在：{key}（无法撤掉）")
            value = self._slots.pop(key)
            self._slot_tokens.pop(key, None)
            self._forget_owner(key)
            await self._settle()
            return value

    def _drop_slot(self, key: str, token: object) -> None:
        """回卷一次服务提供：槽还是自己提供的那份才撤（幂等）。"""
        if self._slot_tokens.get(key) is token:
            self._slot_tokens.pop(key, None)
            self._slots.pop(key, None)
            self._slot_owners.pop(key, None)

    def _forget_owner(self, key: str) -> None:
        """retract 时把槽从提供者插件的 `provided` 里摘掉。"""
        owner_name = self._slot_owners.pop(key, None)
        if owner_name is None:
            return
        owner = self._plugins.get(owner_name)
        if owner is not None and key in owner.provided:
            owner.provided = tuple(item for item in owner.provided if item != key)

    def get(self, key: str, default: Any = None) -> Any:
        """取一项服务；缺失返回 default。

        default 只在缺失时生效，无法区分"没有"与"有但是 None"（用 require 区分）。
        """
        return self._slots.get(key, default)

    def require(self, key: str) -> Any:
        """取一项服务；缺失时抛 MissingServiceError。"""
        value = self._slots.get(key, _MISSING)
        if value is _MISSING:
            raise MissingServiceError(f"服务不存在：{key}（谁提供它？检查装配单）")
        return value

    @property
    def services(self) -> list[str]:
        """当前已提供的服务名（按提供顺序的快照）。"""
        return list(self._slots)

    # ------------------------------------------------------------------
    # effect（注册即回卷）
    # ------------------------------------------------------------------

    async def effect(self, name: str, install: Install) -> Disposer:
        """装载一个 effect：执行 install，登记返回的撤销函数，返回卸载句柄。

        装载期在 ctx 上的登记（provide、嵌套 effect、on）自动挂进本 effect，卸载
        时逐层回卷。install 抛错或返回值不是 None / 可调用对象时，本 effect 与
        子 effect 全部回卷、名字从台账移除，再抛出原始异常。

        返回值若已在账本里（例如 on 返回的退订函数），不重复记账。
        install 可以是同步或异步函数；返回的 disposer 也可以是异步函数。
        name 以 plugin: 或 inject: 开头时报错（内部命名空间）。
        """
        async with self._change():
            return await self._effect(name, install, internal=False)

    async def unload_effect(self, name: str) -> None:
        """卸载一个 effect 及其子 effect；不存在时抛 MissingEffectError。

        plugin: 开头的 effect 不能直接卸载（用 unload_plugin / disable_plugin）。
        某个 disposer 抛错时继续回卷其余部分，最后抛第一个异常。
        """
        if name.startswith(PLUGIN_EFFECT_PREFIX):
            raise ValueError(
                f"插件自己的 effect 不能直接卸载：{name}"
                "（请用 unload_plugin / disable_plugin——它要同步改台账状态）"
            )
        await self._unload_named(name)

    @property
    def effects(self) -> list[str]:
        """当前装载的 effect 名（按装载顺序，含插件自己的 plugin:<名>）。"""
        return list(self._effects)

    # ------------------------------------------------------------------
    # 事件（广播 / 瀑布）
    # ------------------------------------------------------------------

    def on(self, event: str, handler: EventHandler) -> Disposer:
        """订阅事件，返回幂等退订函数。

        emit 的 handler 调用形式为 handler(**payload)，waterfall 为 handler(value, **context)。
        同一事件按注册顺序派发。装载期订阅自动随 effect 回卷。

        派发名单在派发开始时取快照：本轮中途的订阅 / 退订从下一轮生效。
        """
        if not isinstance(event, str) or not event:
            raise ValueError(f"事件名必须是非空字符串，收到：{event!r}")
        if not callable(handler):
            raise TypeError(f"事件 handler 必须可调用，收到：{handler!r}")
        self._handler_seq += 1
        token = self._handler_seq
        self._handlers.setdefault(event, []).append((token, handler))

        def off() -> None:
            handlers = self._handlers.get(event)
            if handlers:
                self._handlers[event] = [item for item in handlers if item[0] != token]

        if self._stack:  # 装载期：登记自动退订（卸载时把它摘掉）
            self._stack[-1].disposers.append(off)
        return off

    async def emit(self, event: str, **payload: Any) -> None:
        """广播事件：按订阅顺序调用 handler(**payload)，返回值忽略。

        handler 可以是同步或异步函数；抛错直接外抛，已跑过的不回滚、后面的不再运行。
        """
        handlers = list(self._handlers.get(event, ()))
        for _, handler in handlers:
            await _maybe_await(handler(**payload))

    async def waterfall(self, event: str, value: Any, **context: Any) -> Any:
        """瀑布事件：按订阅顺序调用 handler(value, **context)。

        handler 可以是同步或异步函数；返回 None 表示不干预，返回其他值则替换当前值。
        异常与订阅名单快照语义同 emit。
        """
        handlers = list(self._handlers.get(event, ()))
        for _, handler in handlers:
            result = await _maybe_await(handler(value, **context))
            if result is not None:
                value = result
        return value

    @property
    def event_names(self) -> list[str]:
        """当前有订阅者的事件名（按首次订阅顺序）。"""
        return [name for name, handlers in self._handlers.items() if handlers]

    # ------------------------------------------------------------------
    # 动态依赖（inject）：依赖齐了才跑一块
    # ------------------------------------------------------------------

    async def inject(self, deps: Iterable[str], callback: Callable[[Context], Any]) -> Disposer:
        """注册一块依赖出现才运行的代码，返回幂等的取消订阅句柄。

        与静态 inject 的区别：静态 inject 缺依赖时整个插件不激活；这里的回调放进
        子 effect 运行，依赖消失自动拆，依赖再次出现时重跑。

        装载期注册时，订阅本身随外层 effect 回卷。回调抛错时该订阅标记 failed 且
        不原地重试，依赖变化后才会重试。deps 必须是非空序列。

        返回的取消订阅句柄是异步函数：`await handle()`。
        """
        names = self._inject_deps(deps)
        if not callable(callback):
            raise TypeError(f"inject 的 callback 必须可调用，收到：{callback!r}")

        async with self._change():
            self._injection_seq += 1
            record = _InjectionRecord(
                seq=self._injection_seq,
                deps=names,
                callback=callback,
                owner_effect=self._stack[-1].name if self._stack else None,
            )
            self._injections[record.seq] = record

            async def handle() -> None:
                # 认"这一次注册"（记录身份），不是序号（同句柄语义）。
                if self._injections.get(record.seq) is not record:
                    return
                async with self._change():
                    if self._injections.get(record.seq) is not record:
                        return
                    await self._cancel_injection(record)
                    if not self._stack:
                        await self._settle()

            if self._stack:  # 装载期：订阅随外层 effect 回卷
                self._stack[-1].disposers.append(handle)
            await self._settle()
        return handle

    @property
    def injections(self) -> list[InjectionInfo]:
        """全部动态依赖订阅的状态快照（按注册顺序）。"""
        return [
            InjectionInfo(
                name=f"{INJECT_EFFECT_PREFIX}{record.seq}",
                deps=record.deps,
                status=record.status,
                missing=tuple(dep for dep in record.deps if dep not in self._slots),
                error=record.error,
            )
            for record in self._injections.values()
        ]

    # ------------------------------------------------------------------
    # 插件（装载 / 卸载 / 停用 / 启用）
    # ------------------------------------------------------------------

    async def load_plugin(self, plugin: object) -> Disposer:
        """装载一个插件，返回卸载函数。

        返回时插件可能 active，也可能 pending（依赖缺失，待就绪后自动激活）。
        同名已存在时抛 DuplicatePluginError。

        激活失败分两种：本次装载触发的失败直接抛出且记录不留台账；由其他操作
        （如 provide）触发的失败从触发方抛出，记录以 failed 保留，可 enable_plugin
        重试。若失败来自被这次装载连带激活的**别的**插件，本插件可能已经激活并
        留在台账——装配方（loader）需要按名字回滚它。
        """
        name = plugin_name(plugin)
        record = _PluginRecord(
            name=name,
            setup=plugin_setup(plugin),
            inject=plugin_injects(plugin),
            optional=plugin_optionals(plugin),
            requires=plugin_requires(plugin),
        )
        async with self._change():
            if name in self._plugins:
                raise DuplicatePluginError(
                    f"插件已装载：{name}（一个运行时里一个插件名只留一份实例；先 unload_plugin）"
                )
            self._plugins[name] = record
            try:
                await self._settle()
            except BaseException:
                if record.status == "failed":
                    self._plugins.pop(name, None)
                raise

        async def handle() -> None:
            # 认"这一次装载"（记录身份），不是名字（见模块开头的「句柄语义」）。
            if self._plugins.get(name) is record:
                await self.unload_plugin(name)

        return handle

    async def unload_plugin(self, name: str) -> None:
        """卸载一个插件：依赖它的活跃插件先退位（深的先退），再回卷它自己。

        退位的依赖者退回 pending，提供者回来时自动重新激活。
        某个插件清理抛错时继续卸载其余部分，最后抛第一个异常。
        插件不存在时抛 MissingPluginError。
        """
        errors: list[BaseException] = []
        async with self._change():
            record = self._plugins.pop(name, None)
            if record is None:
                raise MissingPluginError(f"插件不存在：{name}（无法卸载）")
            dependents = list(reversed(self._dependent_records(record)))
            self._suppressed.update(dep.name for dep in dependents)
            try:
                for dependent in dependents:
                    await self._deactivate_into(dependent, errors)
                await self._deactivate_into(record, errors)
            finally:
                for dependent in dependents:
                    self._suppressed.discard(dependent.name)
            settle_error = await self._settle_collecting()
        if errors:
            if settle_error is not None:
                errors[0].add_note(f"另有异常：{settle_error!r}")
            raise errors[0]
        if settle_error is not None:
            raise settle_error

    async def disable_plugin(self, name: str) -> list[str]:
        """停用插件：回卷 effect、状态标为 disabled，但保留登记。

        与卸载的区别：enable_plugin 用同一个对象恢复，setup 会重跑。
        依赖它的插件一起退位。返回受影响的插件名（含自己）。
        """
        errors: list[BaseException] = []
        async with self._change():
            record = self._plugins.get(name)
            if record is None:
                raise MissingPluginError(f"插件不存在：{name}（无法停用）")
            if record.status == "disabled":
                raise ValueError(f"插件已处于停用状态：{name}")
            dependents = list(reversed(self._dependent_records(record)))
            # 先改自己的状态：回卷途中若触发 settle，它不能把自己当作 pending 复活。
            record.status = "disabled"
            self._suppressed.update(dep.name for dep in dependents)
            try:
                for dependent in dependents:
                    await self._deactivate_into(dependent, errors)
                await self._deactivate_into(record, errors, status="disabled")
            finally:
                for dependent in dependents:
                    self._suppressed.discard(dependent.name)
            settle_error = await self._settle_collecting()
        if errors:
            if settle_error is not None:
                errors[0].add_note(f"另有异常：{settle_error!r}")
            raise errors[0]
        if settle_error is not None:
            raise settle_error
        return [record.name, *(dep.name for dep in dependents)]

    async def enable_plugin(self, name: str) -> None:
        """重新启用 disabled / failed 的插件：标回 pending，由稳定化决定是否激活。

        依赖缺失时停在 pending；其他状态调用会报错。
        """
        async with self._change():
            record = self._plugins.get(name)
            if record is None:
                raise MissingPluginError(f"插件不存在：{name}（无法启用）")
            if record.status not in ("disabled", "failed"):
                raise ValueError(
                    f"插件当前是 {record.status}，不需要启用（只有 disabled / failed 可以）"
                )
            record.status = "pending"
            record.error = None
            await self._settle()

    @property
    def plugins(self) -> list[PluginInfo]:
        """全部插件的状态快照（按装载顺序）。"""
        return [
            PluginInfo(
                name=record.name,
                status=record.status,
                inject=record.inject,
                missing=tuple(dep for dep in record.inject if dep not in self._slots),
                optional=record.optional,
                missing_optionals=tuple(
                    dep for dep in record.optional if dep not in self._slots
                ),
                provided=record.provided,
                error=record.error,
                requires=record.requires,
                missing_requires=self._missing_requires(record),
            )
            for record in self._plugins.values()
        ]

    def _missing_requires(self, record: _PluginRecord) -> tuple[str, ...]:
        """插件级依赖里当前不在台账的那些插件名。"""
        names = list(self._plugins)
        return tuple(
            required
            for required in record.requires
            if not any(matches_plugin_requirement(required, name) for name in names)
        )

    @property
    def active_plugins(self) -> list[str]:
        """运行中的插件名（按装载顺序）。"""
        return [rec.name for rec in self._plugins.values() if rec.status == "active"]

    @property
    def pending_plugins(self) -> list[str]:
        """等待依赖的插件名（按装载顺序）。"""
        return [rec.name for rec in self._plugins.values() if rec.status == "pending"]

    @property
    def disabled_plugins(self) -> list[str]:
        """被停用的插件名（按装载顺序）。"""
        return [rec.name for rec in self._plugins.values() if rec.status == "disabled"]

    def missing_dependencies(self, name: str) -> list[str]:
        """某个插件当前缺失的必需依赖；插件不存在时报错。"""
        record = self._plugins.get(name)
        if record is None:
            raise MissingPluginError(f"插件不存在：{name}")
        return [dep for dep in record.inject if dep not in self._slots]

    # ------------------------------------------------------------------
    # 内部：effect 装载 / 回卷
    # ------------------------------------------------------------------

    async def _effect(self, name: str, install: Install, *, internal: bool) -> Disposer:
        """effect 与插件装载共用的实现；internal 放行内部命名空间前缀。"""
        if not isinstance(name, str) or not name:
            raise ValueError(f"effect 名必须是非空字符串，收到：{name!r}")
        if name.startswith(PLUGIN_EFFECT_PREFIX) and not internal:
            raise ValueError(
                f"effect 名不能以 {PLUGIN_EFFECT_PREFIX!r} 开头：{name!r}"
                "（那是插件自己的命名空间，由 load_plugin 使用）"
            )
        if name.startswith(INJECT_EFFECT_PREFIX) and not internal:
            raise ValueError(
                f"effect 名不能以 {INJECT_EFFECT_PREFIX!r} 开头：{name!r}"
                "（那是动态依赖订阅的命名空间，由 ctx.inject 使用）"
            )
        if name in self._effects:
            raise DuplicateEffectError(f"effect 已存在：{name}（先卸载再重装）")

        record = _Effect(name=name, parent=self._stack[-1] if self._stack else None)
        self._effects[name] = record
        if record.parent is not None:
            record.parent.children.append(record)

        self._stack.append(record)
        try:
            result = await _maybe_await(install())
            if result is not None and not callable(result):
                raise TypeError(
                    f"effect {name!r} 的 install 必须返回 None 或撤销函数，收到：{result!r}"
                )
        except BaseException:
            # 装载失败：把已登记的（含子 effect）撤销掉再抛——不留半装状态。
            self._stack.pop()
            self._detach(record)
            self._forget(record)
            await self._unwind(record)  # 回卷途中的异常不遮蔽原始异常
            raise
        self._stack.pop()
        if result is not None and not any(result is item for item in record.disposers):
            # install 的返回值 = "它自己资源的撤销动作"。若已在账本里（如
            # `lambda: ctx.on(...)` 返回的退订——on 早已自动登记），不重复记账。
            record.disposers.append(result)
        await self._settle()  # 本次装载可能解开了别的插件的依赖（重入时自动跳过）

        async def handle() -> None:
            # 认"这一次装载"（记录身份），不是名字（见模块开头的「句柄语义」）。
            if self._effects.get(name) is record:
                await self._unload_named(name)

        return handle

    async def _unload_named(self, name: str) -> None:
        """按名字回卷、稳定化，并重抛第一个清理异常。"""
        async with self._change():
            errors = await self._unload_effect(name)
            await self._settle()
        if errors:
            raise errors[0]

    async def _unload_effect(self, name: str) -> list[BaseException]:
        """按名字回卷一个 effect，返回途中的异常。"""
        record = self._effects.get(name)
        if record is None:
            raise MissingEffectError(f"effect 不存在：{name}（无法卸载）")
        self._detach(record)  # 先与父链断开：父之后卸载时不会重复回卷它
        self._forget(record)  # 从台账摘掉整棵子树（不动 children——回卷要用）
        return await self._unwind(record)

    @staticmethod
    def _detach(record: _Effect) -> None:
        """把 effect 从父的 children 中摘掉，避免重复回卷。"""
        if record.parent is not None and record in record.parent.children:
            record.parent.children.remove(record)

    def _forget(self, record: _Effect) -> None:
        """从名字台账中移除一个 effect 及其子树。"""
        self._effects.pop(record.name, None)
        for child in record.children:
            self._forget(child)

    async def _unwind(self, record: _Effect) -> list[BaseException]:
        """回卷：子 effect 先（同级后进先出），再跑自己的撤销函数（后进先出）。

        返回途中的异常；一个失败不打断其余回卷。
        """
        errors: list[BaseException] = []
        self._unwinding += 1
        try:
            while record.children:
                child = record.children.pop()  # 同级后进先出
                errors.extend(await self._unwind(child))
            while record.disposers:
                disposer = record.disposers.pop()  # 后进先出
                try:
                    await _maybe_await(disposer())
                except BaseException as exc:  # 全部捕获、统一重抛
                    errors.append(exc)
        finally:
            self._unwinding -= 1
        return errors

    # ------------------------------------------------------------------
    # 内部：激活 / 退位 / 稳定化
    # ------------------------------------------------------------------

    def _deps_ok(self, record: _PluginRecord) -> bool:
        return all(dep in self._slots for dep in record.inject)

    @staticmethod
    def _inject_deps(deps: Iterable[str]) -> tuple[str, ...]:
        """校验并规范化动态依赖声明。"""
        if isinstance(deps, (str, bytes)):
            raise TypeError(f"inject 的 deps 必须是序列（如 ['store']），收到字符串：{deps!r}")
        if not isinstance(deps, Iterable):
            raise TypeError(f"inject 的 deps 不可迭代：{deps!r}")
        names = tuple(str(dep) for dep in deps)
        if not names:
            raise ValueError("inject 至少要声明一个依赖")
        if any(not name for name in names):
            raise ValueError(f"inject 的 deps 里有空的服务名：{names!r}")
        if len(set(names)) != len(names):
            raise ValueError(f"inject 的 deps 有重复项：{names!r}")
        return names

    async def _start_injection(self, record: _InjectionRecord) -> None:
        """依赖齐了：把回调放进一个子 effect 里运行。"""
        effect_name = f"{INJECT_EFFECT_PREFIX}{record.seq}"
        record.status = "active"
        record.error = None
        try:
            await self._effect(effect_name, lambda: record.callback(self), internal=True)
        except BaseException as exc:
            # 回调抛错：标 failed、不原地重试（否则每次 settle 都要再炸一遍）。
            record.status = "failed"
            record.error = exc
            record.effect = None
            raise
        record.effect = effect_name
        self._report_injections_started.append(effect_name)

    async def _stop_injection(self, record: _InjectionRecord) -> None:
        """依赖不满足：回卷回调的 effect，保留订阅。"""
        effect_name, record.effect = record.effect, None
        record.status = "pending"
        record.error = None
        if effect_name is not None:
            self._report_injections_stopped.append(effect_name)
            errors = await self._unload_effect(effect_name)
            if errors:
                raise errors[0]  # 清理失败 fail loud（其余回卷照常完成）

    async def _cancel_injection(self, record: _InjectionRecord) -> None:
        """取消订阅：回卷回调的 effect，并从订阅表移除。"""
        self._injections.pop(record.seq, None)
        await self._stop_injection(record)

    async def _activate(self, record: _PluginRecord) -> None:
        """激活插件：把 setup 放进 plugin:<名> 的 effect 里运行。

        setup 中的登记都挂在该 effect 下。失败时标记 failed、还原服务表并抛出。
        """
        before = dict(self._slots)
        before_tokens = dict(self._slot_tokens)
        before_owners = dict(self._slot_owners)
        before_provided = {name: item.provided for name, item in self._plugins.items()}
        effect_name = f"{PLUGIN_EFFECT_PREFIX}{record.name}"
        self._activating = record
        try:
            await self._effect(effect_name, lambda: record.setup(self), internal=True)
        except BaseException as exc:
            record.status = "failed"
            record.error = exc
            # 保险：装载失败 = 服务表（含令牌 / 提供者）回到装载前
            self._slots = before
            self._slot_tokens = before_tokens
            self._slot_owners = before_owners
            for name, provided in before_provided.items():
                owner = self._plugins.get(name)
                if owner is not None:
                    owner.provided = provided
            raise
        finally:
            self._activating = None
        record.effect = effect_name
        # 本插件在 active 期间新提供的服务（按提供顺序）——退位级联按它扩散。
        record.provided = tuple(key for key in self._slots if key not in before)
        record.status = "active"
        record.error = None
        self._report_activated.append(record.name)

    async def _deactivate(self, record: _PluginRecord, *, status: PluginStatus = "pending") -> None:
        """回卷插件：卸载它的 effect，更新状态与服务表。

        不推动稳定态；调用方负责收尾。
        """
        record.status = status
        record.error = None
        record.provided = ()
        effect_name, record.effect = record.effect, None
        if effect_name is not None:
            self._report_retired.append(record.name)
            errors = await self._unload_effect(effect_name)
            if errors:
                raise errors[0]  # 清理失败 fail loud（其余回卷照常完成）

    async def _deactivate_into(
        self,
        record: _PluginRecord,
        errors: list[BaseException],
        *,
        status: PluginStatus = "pending",
    ) -> None:
        """退位并把清理异常收进 errors（供级联使用）。"""
        try:
            await self._deactivate(record, status=status)
        except BaseException as exc:
            errors.append(exc)

    async def _settle_collecting(self) -> BaseException | None:
        """跑一次稳定化，返回它抛出的第一个异常（没有则 None）。"""
        try:
            await self._settle()
        except BaseException as exc:
            return exc
        return None

    def _dependent_closure(
        self, going: Iterable[str], *, exclude: _PluginRecord | None = None
    ) -> list[_PluginRecord]:
        """按服务依赖扩散，返回需要退位的活跃插件（由近及远）。

        `going` 是即将消失的服务名集合：谁 inject 了它们，谁就要退位；退位者
        提供的服务再加入集合。只考虑活跃插件；环形依赖在同一批被选中，不会
        无限循环。`exclude` 用于"提供者自己不算依赖者"的场景。
        """
        selected: list[_PluginRecord] = []
        reachable = set(going)
        remaining = {
            name: record
            for name, record in self._plugins.items()
            if record.status == "active" and record is not exclude
        }
        changed = True
        while changed:
            changed = False
            for name, record in list(remaining.items()):
                if any(dep in reachable for dep in record.inject):
                    selected.append(record)
                    reachable.update(record.provided)
                    del remaining[name]
                    changed = True
        return selected

    def _dependent_records(self, provider: _PluginRecord) -> list[_PluginRecord]:
        """需要随 provider 退位的活跃插件（不含自己），由近及远。"""
        return self._dependent_closure(provider.provided, exclude=provider)

    async def _retire_unsatisfied(self, errors: list[BaseException]) -> bool:
        """退位所有依赖已不满足的活跃插件，顺序"深的先退"；返回是否退位过。

        清理异常收进 `errors` 并继续退位，全部退完后再由 `_settle` 抛出第一个
        异常——不退到一半就把世界留在"active 但缺依赖"的中间态。
        """
        missing = {
            dep
            for record in self._plugins.values()
            if record.status == "active"
            for dep in record.inject
            if dep not in self._slots
        }
        if not missing:
            return False
        affected = self._dependent_closure(missing)
        if not affected:
            return False
        # 闭包由近及远；反转即"深的先退"，依赖者清理时它的服务都还在。
        for record in reversed(affected):
            await self._deactivate_into(record, errors)
        return True

    async def _settle(self) -> None:
        """把插件世界推到稳定态：先退依赖不再满足的，再激活依赖已满足的。

        触发时机：服务与插件表变化、effect 装卸。三种跳过：正在稳定化中（最外层
        循环会继续扫描）、装载期（栈非空）、回卷期（disposer 还在跑）——后两者
        都由本次操作的收尾再推一次。

        退位"深的先退"；清理 / 激活 / 注入启动的异常全部收齐，世界推到稳定态
        后再抛第一个（其余进异常的 note）。激活失败的记录以 failed 保留。
        """
        if self._settling or self._stack or self._unwinding:
            return
        self._settling = True
        errors: list[BaseException] = []
        try:
            while True:
                changed = False
                # 动态依赖：先停"依赖不再满足"的（订阅保留），再在最后启动"依赖已齐"的。
                for injection in list(self._injections.values()):
                    deps_ok = all(dep in self._slots for dep in injection.deps)
                    if injection.status == "active" and not deps_ok:
                        try:
                            await self._stop_injection(injection)
                        except BaseException as exc:
                            errors.append(exc)
                        changed = True
                    elif injection.status == "failed" and not deps_ok:
                        # 依赖走了：回到 pending，等它再出现时重试（不原地空转）。
                        injection.status = "pending"
                        injection.error = None
                        changed = True
                if await self._retire_unsatisfied(errors):
                    changed = True
                for record in list(self._plugins.values()):
                    if (
                        record.status == "pending"
                        and record.name not in self._suppressed
                        and self._deps_ok(record)
                    ):
                        try:
                            await self._activate(record)
                        except BaseException as exc:
                            errors.append(exc)
                        changed = True
                for injection in list(self._injections.values()):
                    if injection.status == "pending" and all(
                        dep in self._slots for dep in injection.deps
                    ):
                        try:
                            await self._start_injection(injection)
                        except BaseException as exc:
                            errors.append(exc)
                        changed = True
                if not changed:
                    break
        except BaseException as exc:
            for error in errors:
                exc.add_note(f"另有异常：{error!r}")
            raise
        finally:
            self._settling = False
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"另有异常：{error!r}")
            raise errors[0]
