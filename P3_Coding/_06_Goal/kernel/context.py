"""Context —— 插件内核：服务槽位 + 事件总线 + effect 台账 + 插件生命周期。

P2 `_07` 的 Context 只有"命名槽位 + effect 台账"两件；P3 把它补成完整的
**ctx 服务 + inject + 事件 + effect + 加载/卸载**五件套（路线图里 P4 的内核
目标，提前到 P3 作为基础包落地）：

**服务**（provide / retract / require / get）
    命名单槽；重复提供 fail loud（不静默覆盖）；require 缺失 fail loud。
    装载期（effect 的 install 执行中）提供的一切会自动挂上"撤销槽位"的
    disposer —— 插件不用自己写撤销动作。

**事件**（on / emit / waterfall）
    订阅返回退订函数；装载期订阅自动挂进当前 effect（随卸载回卷）。
    `emit` 是广播（handler(**payload)）；`waterfall` 是可改写值的中间件链
    （handler(value, **context) 返回非 None 即替换当前值）——后者是"每 step
    改写 LLM 请求""开会话时贡献初始快照"这类协作的载体。

**effect**（effect / register / unload）
    注册即回卷：装载期发生的一切副作用（provide / on / 嵌套 effect）自动
    登记撤销；卸载按"子先父后、同级后进先出"回卷；某个 disposer 抛错不
    打断其余回卷（最后抛第一个异常）。装载失败不留半装状态。

**插件与 inject**（load_plugin / load_plugins / unload_plugin）
    *插件 = 有 setup(ctx) 的对象（或普通函数），可用 name/inject 声明身份
    与依赖*。inject 是服务名列表，装载器据此维护插件生命周期：

    - 依赖齐备 → 立即激活（setup 在 effect 里执行）；
    - 依赖缺失 → 保持 **pending**，等服务就绪自动激活（**装载顺序无关**）；
    - 依赖的服务消失（被提供方卸载/退役）→ 自动**停用**（effect 回卷，
      退回 pending）；服务再出现 → 自动**重新激活**（setup 重跑）。

    这就是"加载/卸载"的完整语义（Cordis fiber 依赖跟踪的最小版）：
    插件世界里没有"装载顺序"这个隐含条件，依赖关系本身驱动一切。

对应 dsh：`docs/cordis-primer.md`（服务仓库 / registrations are reversible
effects / inject）。本实现刻意朴素：同步、无异步 fiber、无事件优先级。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from kernel.plugin import plugin_injects, plugin_name, plugin_optionals, plugin_setup

# 装载函数：执行副作用，返回撤销函数（或 None 表示"副作用都走 ctx 自动管理"）。
Install = Callable[[], "Callable[[], None] | None"]
Disposer = Callable[[], None]
EventHandler = Callable[..., Any]

PluginStatus = Literal["pending", "active", "failed"]


class DuplicateSlotError(ValueError):
    """同名槽位 / effect / 插件重复注册（fail loud，不静默覆盖）。"""


class UnknownEffectError(KeyError):
    """卸载一个不存在的 effect（fail loud，防止拼写错误静默生效）。"""


class DuplicatePluginError(ValueError):
    """同名插件重复装载（fail loud；要换实现先 unload_plugin）。"""


class UnknownPluginError(KeyError):
    """卸载一个不存在的插件（fail loud）。"""


@dataclass
class _Effect:
    """一个已装载（或正在装载）的 effect：名字 + disposer + 子 effect + 父引用。"""

    name: str
    disposers: list[Disposer] = field(default_factory=list)
    children: list[_Effect] = field(default_factory=list)
    parent: _Effect | None = None


@dataclass(frozen=True)
class PluginInfo:
    """一个插件的当前状态快照（观测用：demo / 测试 / 入口校验）。"""

    name: str
    status: PluginStatus
    inject: tuple[str, ...]
    missing: tuple[str, ...]  # 当前未满足的**必需**依赖（status=pending 时有意义）
    #: 可选依赖声明（`_05`）：表里列出、缺了不禁用——missing_optionals 是当前
    #: 取不到的那些（界面/台账据此显示"降级中"，而不是假装一切正常）。
    optional: tuple[str, ...] = ()
    missing_optionals: tuple[str, ...] = ()
    error: BaseException | None = None


@dataclass
class _PluginRecord:
    """插件台账里的一条记录（生命周期状态的唯一事实）。"""

    name: str
    setup: Callable[[Context], Disposer | None]
    inject: tuple[str, ...]
    #: 可选依赖：**不参与 `_deps_ok`**——缺了照常激活、来了不重装（见
    #: kernel/plugin.py 的语义表）。这里存着主要是为了可见性（PluginInfo）。
    optional: tuple[str, ...] = ()
    status: PluginStatus = "pending"
    disposer: Disposer | None = None  # 激活时拿到的 effect 卸载句柄
    error: BaseException | None = None
    provided: tuple[str, ...] = ()  # 激活期间提供的服务（退位顺序要用）


class Context:
    """插件共享的注册中心（ctx）：服务 + 事件 + effect + 插件台账。

    用法（插件作者视角）：

        class MyPlugin:
            name = "my-plugin"
            inject = ("sessions",)          # 依赖的服务（缺失则先挂起）

            def setup(self, ctx):
                sessions = ctx.require("sessions")   # 依赖已保证就绪
                ctx.provide("greeting", "你好")       # 注册即 effect（卸载自动回卷）
                return None                           # 或返回显式撤销函数

    用法（装载器视角）：

        ctx = Context()
        load_plugins(ctx, [MyPlugin(), OtherPlugin()])   # 顺序无关（inject 驱动）
        ctx.unload_plugin("my-plugin")                   # 卸载：回卷 + 依赖者停用
    """

    def __init__(self) -> None:
        self._slots: dict[str, Any] = {}
        self._effects: dict[str, _Effect] = {}
        self._stack: list[_Effect] = []  # 正在装载的 effect 栈（父子关系用）
        self._auto = 0  # register() 的自动编号
        self._handlers: dict[str, list[tuple[int, EventHandler]]] = {}
        self._handler_seq = 0
        self._plugins: dict[str, _PluginRecord] = {}
        self._settling = False  # settle 重入保护（见 _settle）
        self._suppressed: set[str] = set()  # 退位 cascade 中禁止重激活的插件名

    # ------------------------------------------------------------------
    # 服务（命名单槽）
    # ------------------------------------------------------------------

    def provide(self, key: str, value: Any) -> None:
        """提供一项服务；同名重复 → DuplicateSlotError（fail loud，不静默覆盖）。

        **注册即 effect**：在某个 effect 的装载期调用时，本次提供会自动挂上
        "撤销槽位"的 disposer——插件崩了、effect 卸载了，槽位都会被回卷。
        effect 之外直接调用则不自动登记（由调用方自己管理）。
        """
        if key in self._slots:
            raise DuplicateSlotError(f"服务已提供：{key}（重复注册直接报错，不静默覆盖）")
        self._slots[key] = value
        if self._stack:  # 装载期：自动登记的撤销动作（幂等 pop，防与显式撤销重复）
            self._stack[-1].disposers.append(lambda: self._slots.pop(key, None))
        self._settle()

    def retract(self, key: str) -> Any:
        """撤销一项服务并返回原值；不存在 → KeyError（fail loud）。

        依赖它的活跃插件会在 settle 中自动停用（退回 pending）。
        """
        if key not in self._slots:
            raise KeyError(f"服务不存在：{key}（无法撤销）")
        value = self._slots.pop(key)
        self._settle()
        return value

    def get(self, key: str, default: Any = None) -> Any:
        """取一项服务；缺省返回 default（"可选依赖"用这个，如可选的 shell 能力）。"""
        return self._slots.get(key, default)

    def require(self, key: str) -> Any:
        """取一项服务；缺失 → KeyError（"必须有"用这个，声明进 inject 更佳）。"""
        if key not in self._slots:
            raise KeyError(f"服务未提供：{key}（检查提供它的插件是否已装载）")
        return self._slots[key]

    @property
    def services(self) -> list[str]:
        """当前已提供的服务名（按提供顺序）。"""
        return list(self._slots)

    # ------------------------------------------------------------------
    # effect：注册即回卷单元
    # ------------------------------------------------------------------

    def effect(self, name: str, install: Install) -> Disposer:
        """装载一个 effect：执行 install，登记它返回的 disposer。

        返回一个**卸载函数**（调用即回卷本 effect——含它的子 effect——并注销台账）。
        name 重复 → DuplicateSlotError。装载期（install 执行中）注册的其它 effect
        自动成为子 effect：卸载时按"子先父后、同级后进先出"回卷。
        """
        if name in self._effects:
            raise DuplicateSlotError(f"effect 已存在：{name}（先 unload 再重装）")
        record = _Effect(name=name, parent=self._stack[-1] if self._stack else None)
        self._effects[name] = record
        if record.parent is not None:
            record.parent.children.append(record)

        self._stack.append(record)
        try:
            disposer = install()
        except BaseException:
            # 装载失败：把已登记的（含子 effect）撤销掉再抛——不留半装状态。
            self._stack.pop()
            self._detach(record)
            self._forget(record)
            self._unwind(record)
            raise
        self._stack.pop()
        if disposer is not None:
            record.disposers.append(disposer)
        # 本 effect 装载期提供过的服务可能解开了别的插件的依赖——推向稳定态。
        # （在 _settle 内部再进来时会被重入保护挡下，不会嵌套。）
        self._settle()
        return lambda: self.unload(name)

    def register(self, install: Install) -> Disposer:
        """平铺版 effect：每次调用自动编号一个独立 effect（不关心名字时用）。"""
        self._auto += 1
        return self.effect(f"auto:{self._auto}", install)

    def unload(self, name: str) -> None:
        """卸载一个 effect：先逐层回卷子 effect，再逆序执行自己的 disposer。

        某个 disposer 抛异常时：**继续回卷其余部分**，最后把第一个异常抛出去。
        """
        record = self._effects.get(name)
        if record is None:
            raise UnknownEffectError(f"effect 不存在：{name}（无法卸载）")
        self._detach(record)  # 先与父链断开：父之后卸载时不会重复回卷它
        self._forget(record)  # 从台账摘掉整棵子树（不动 children 结构——回卷要用）
        errors = self._unwind(record)
        self._settle()
        if errors:
            raise errors[0]

    @property
    def effect_names(self) -> list[str]:
        """当前装载的 effect 名（按装载顺序）。"""
        return list(self._effects)

    # ------------------------------------------------------------------
    # 事件（广播 / 瀑布）
    # ------------------------------------------------------------------

    def on(self, event: str, handler: EventHandler) -> Disposer:
        """订阅一个事件；返回退订函数。

        广播事件：`emit` 时调用 `handler(**payload)`；
        瀑布事件：`waterfall` 时调用 `handler(value, **context)`，返回非 None
        即替换当前值。

        装载期（effect 的 install 执行中）订阅会自动挂上退订动作——effect
        卸载时订阅随之消失（注册即 effect 在事件上的落地）。
        """
        self._handler_seq += 1
        token = self._handler_seq
        self._handlers.setdefault(event, []).append((token, handler))

        def dispose() -> None:
            handlers = self._handlers.get(event)
            if handlers:
                self._handlers[event] = [item for item in handlers if item[0] != token]

        if self._stack:
            self._stack[-1].disposers.append(dispose)
        return dispose

    def emit(self, event: str, **payload: Any) -> None:
        """广播一个事件：按订阅顺序调用 `handler(**payload)`（异常直接抛，fail loud）。"""
        for _, handler in list(self._handlers.get(event, ())):
            handler(**payload)

    def waterfall(self, event: str, value: Any, **context: Any) -> Any:
        """瀑布式事件：每个 handler 可返回新值替换当前值（None = 不改）。

        handler 签名：`handler(value, **context) -> Any | None`。
        """
        for _, handler in list(self._handlers.get(event, ())):
            result = handler(value, **context)
            if result is not None:
                value = result
        return value

    @property
    def event_names(self) -> list[str]:
        """当前有订阅者的事件名（订阅者已全部退订的事件不再列出）。"""
        return [name for name, handlers in self._handlers.items() if handlers]

    # ------------------------------------------------------------------
    # 插件（inject 驱动的生命周期）
    # ------------------------------------------------------------------

    def load_plugin(self, plugin: object) -> Disposer:
        """装载一个插件；返回卸载函数（调用即 unload_plugin）。

        返回时插件可能处于 active（依赖齐备）或 pending（依赖缺失，等就绪
        自动激活）——**待激活不是错误**；入口脚本应在装载完一批后检查
        `pending_plugins`，有残留说明依赖写错了（fail loud 的责任在调用方）。
        激活失败（setup 抛错）→ 异常直接抛出，且不留半装状态。
        """
        name = plugin_name(plugin)
        if name in self._plugins:
            raise DuplicatePluginError(f"插件已装载：{name}（先 unload_plugin 再重载）")
        record = _PluginRecord(
            name=name,
            setup=plugin_setup(plugin),
            inject=plugin_injects(plugin),
            optional=plugin_optionals(plugin),
        )
        self._plugins[name] = record
        try:
            self._settle()
        except BaseException:
            if record.status == "failed":
                self._plugins.pop(name, None)  # 失败记录不留在台账（不留半装状态）
            raise
        return lambda: self.unload_plugin(name)

    def load_plugins(self, plugins: Sequence[object]) -> list[Disposer]:
        """按顺序批量装载；任一失败 → 回卷本次已装成功的部分，再把异常抛出。"""
        loaded: list[Disposer] = []
        attempted: list[str] = []
        try:
            for plugin in plugins:
                attempted.append(plugin_name(plugin))
                loaded.append(self.load_plugin(plugin))
        except BaseException:
            for name in reversed(attempted):  # 逆序回卷（后装的先撤）
                record = self._plugins.get(name)
                if record is None:
                    continue
                if record.status == "active":
                    self.unload_plugin(name)
                else:
                    self._plugins.pop(name, None)  # pending/failed：直接丢弃
            raise
        return loaded

    def unload_plugin(self, name: str) -> None:
        """卸载一个插件：**依赖者先退位**，然后回卷它自己的 effect（提供过的服务、
        订阅全部撤销），最后 settle 把整个插件世界推向新的稳定态。

        退位顺序（提供者被卸时）：直接/间接依赖它服务的插件先被停用（深的先退），
        提供者自己最后拆——这样依赖者的清理代码在运行期间，服务仍然可用。
        """
        record = self._plugins.pop(name, None)
        if record is None:
            raise UnknownPluginError(f"插件不存在：{name}（无法卸载）")
        dependents = list(reversed(self._dependent_records(record)))
        # 退位 cascade 期间抑制重激活：依赖者的 effect 回卷会触发 settle，
        # 此刻提供者的槽位尚未撤销——不抑制的话依赖者会被立刻重新激活。
        self._suppressed.update(dep.name for dep in dependents)
        try:
            for dependent in dependents:
                self._deactivate(dependent)
            self._deactivate(record)
        finally:
            for dependent in dependents:
                self._suppressed.discard(dependent.name)
        self._settle()

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
                error=record.error,
            )
            for record in self._plugins.values()
        ]

    @property
    def active_plugins(self) -> list[str]:
        return [rec.name for rec in self._plugins.values() if rec.status == "active"]

    @property
    def pending_plugins(self) -> list[str]:
        return [rec.name for rec in self._plugins.values() if rec.status == "pending"]

    def missing_dependencies(self, name: str) -> list[str]:
        """某个插件当前缺失的依赖服务名（用于 fail-loud 文案 / 观测）。"""
        record = self._plugins.get(name)
        if record is None:
            raise UnknownPluginError(f"插件不存在：{name}")
        return [dep for dep in record.inject if dep not in self._slots]

    # ------------------------------------------------------------------
    # 内部：插件激活 / 停用 / 稳定化
    # ------------------------------------------------------------------

    def _deps_ok(self, record: _PluginRecord) -> bool:
        return all(dep in self._slots for dep in record.inject)

    def _activate(self, record: _PluginRecord) -> None:
        """激活一个插件：setup 放进一个 effect；失败标记 failed 并抛出。

        激活前后对比服务表，记下"这个插件提供了哪些服务"（退位顺序的依据）。
        """
        before = set(self._slots)
        try:
            record.disposer = self.effect(f"plugin:{record.name}", lambda: record.setup(self))
        except BaseException as exc:
            record.status = "failed"
            record.error = exc
            raise
        record.provided = tuple(sorted(set(self._slots) - before))
        record.status = "active"

    def _deactivate(self, record: _PluginRecord) -> None:
        """停用（或丢弃）一个插件记录：回卷它的 effect，退回 pending。"""
        record.status = "pending"
        record.error = None
        record.provided = ()
        disposer, record.disposer = record.disposer, None
        if disposer is not None:
            disposer()  # = ctx.unload("plugin:<name>")，回卷其中一切登记

    def _dependent_records(self, provider: _PluginRecord) -> list[_PluginRecord]:
        """列出需要随 provider 一起退位的插件（**不含 provider 本身**）。

        顺序 = "由近及远"的发现顺序（先直接依赖者、再它们的依赖者）；调用方
        反转后停用即为"深的先退"。服务闭包按层扩展，天然容忍环形依赖
        （环形会在同一批被选中，不无限循环）。
        """
        going = set(provider.provided)
        selected: list[_PluginRecord] = []
        remaining = {
            name: rec
            for name, rec in self._plugins.items()
            if rec.status == "active" and rec is not provider
        }
        changed = True
        while changed:
            changed = False
            for name, rec in list(remaining.items()):
                if any(dep in going for dep in rec.inject):
                    selected.append(rec)
                    going.update(rec.provided)
                    del remaining[name]
                    changed = True
        return selected

    def _settle(self) -> None:
        """把插件世界推到稳定态：先撤"依赖不再满足"的，再激活"依赖已满足"的。

        在以下时机被触发：provide / retract / effect.unload / load_plugin /
        unload_plugin。重入保护：在 effect 装载中（栈非空）或已在 settle 中
        时直接返回——最外层调用者收尾时再来一趟，保证激活不会嵌套在别人的
        effect 里（插件 effect 永远平铺在根层）。
        """
        if self._settling or self._stack:
            return
        self._settling = True
        try:
            while True:
                changed = False
                for record in list(self._plugins.values()):
                    if record.status == "active" and not self._deps_ok(record):
                        self._deactivate(record)
                        changed = True
                for record in list(self._plugins.values()):
                    if (
                        record.status == "pending"
                        and record.name not in self._suppressed
                        and self._deps_ok(record)
                    ):
                        self._activate(record)
                        changed = True
                if not changed:
                    return
        finally:
            self._settling = False

    # ------------------------------------------------------------------
    # 内部：effect 台账维护（_detach / _forget / _unwind）
    # ------------------------------------------------------------------

    @staticmethod
    def _detach(record: _Effect) -> None:
        """把 effect 从父的 children 列表里摘掉（父链断开，防重复回卷）。"""
        if record.parent is not None and record in record.parent.children:
            record.parent.children.remove(record)

    def _forget(self, record: _Effect) -> None:
        """从台账里摘掉一个 effect（连同它的子树）。

        只动 `self._effects` 这张名字表：**不碰 children 结构**——回卷
        （_unwind）要靠它逐层展开子 effect。
        """
        self._effects.pop(record.name, None)
        for child in record.children:
            self._forget(child)

    @staticmethod
    def _unwind(record: _Effect) -> list[BaseException]:
        """回卷：子 effect 先（后进先出），然后自己的 disposer（后进先出）。

        返回途中捕获的异常（不错过其余回卷）。
        """
        errors: list[BaseException] = []
        while record.children:
            child = record.children.pop()  # 同级后进先出
            errors.extend(Context._unwind(child))
        while record.disposers:
            disposer = record.disposers.pop()  # LIFO：后进先出
            try:
                disposer()
            except BaseException as exc:  # noqa: BLE001 —— 全部捕获后统一重抛
                errors.append(exc)
        return errors


__all__ = [
    "Context",
    "Disposer",
    "DuplicatePluginError",
    "DuplicateSlotError",
    "EventHandler",
    "Install",
    "PluginInfo",
    "PluginStatus",
    "UnknownEffectError",
    "UnknownPluginError",
]
