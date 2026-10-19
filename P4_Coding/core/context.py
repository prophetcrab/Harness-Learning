"""Context —— 协作面（服务槽位 + 事件）+ 生命周期台账（插件状态机 + effect 回卷）。

**服务槽位**（M1 步 1）：命名单槽的提供与查找——插件之间唯一的协作通道。
**插件装载**（M1 步 2）：把"一个插件对象"变成台账里一条受依赖驱动的记录：
装载顺序无关；依赖齐备自动激活、提供者走了自动退位；disable / enable 保留登记。
**effect 回卷**（M1 步 3）：**注册即回卷**——装载期做的一切登记（提供的服务、
嵌套 effect、订阅、插件自己的资源）自动挂上撤销动作，卸载时逐层回卷。
**事件**（M1 步 4，本步）：广播（emit）与瀑布（waterfall）——插件之间的**第二条
通道**。服务用来"用别人的能力"，事件用来"观察与拦截"；瀑布的中间件语义就是
策略插座的本体（超时、预算这类 guard 将来挂在这里）。

三条不变式：

    fail loud    重名槽 / 缺失槽 / 重名插件 / 重名 effect / 非法声明 / handler 抛错
                 → 直接报错（总线不替消费方吞错或隔离）
    不留半装     装载失败 → 它登记过的一切被回卷、服务表还原（等于"什么都没发生"）
    回卷干净     回卷顺序"子先父后、同级后进先出"；一个 disposer 抛错**不打断**
                 其余回卷（全部跑完，最后重抛第一个异常）——**跨插件的级联同理**：
                 一次卸载里某个插件的清理炸了，其余照常退位（错误最后抛出）

effect 的两级用法：

    ctx.effect("名字", install)   自己的账：把一组登记打包成可撤销单元
    插件装载                      插件 = 一个 effect（`plugin:<名字>`）：
                                  setup 期间的一切都挂在这个 effect 下

**句柄语义**：装载返回的卸载函数认的是"**这一次装载**"，不是名字——
重复调用、对应对象已被回卷（如子 effect 随父回卷）、或已被同名新对象顶替，
都是**安全空操作**；而按名字的 API（unload_effect / unload_plugin）**严格
fail loud**（名字是地址，写错地址必须报出来）。两者的分工是刻意的：句柄
天然出现在"清理顺序不确定"的场景里（谁先卸谁后卸都可能），名字出现在
人手敲的地方（敲错要立刻知道）。

依赖语义（两组声明，读法见 core/plugin.py）：

    inject    必需：缺了 → pending（依赖齐的那一刻自动激活）；提供者走了 → 自动退位
    optional  可选：缺了 → 照常激活（用 ctx.get 容错）；来了 → 不重装

**变更串行化**（M1 步 7）：所有改变世界的操作走同一把**可重入**锁——
并发请求（两个 HTTP 线程、模型工具线程、界面轮询）不会撕裂台账；重入是
合法的（插件在自己的 setup / 清理里装卸别的插件）。每次**有效的**变更
（服务表 / 插件状态 / effect 表真的变了）revision +1，并留下 `last_change`
报告（谁退位、谁激活）——界面轮询与测试据此判断"变过没有"。

还没做的（按 PLAN 的 M1 拆步表）：启动器（步 8）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
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

#: effect 的装载函数：执行登记，返回撤销函数（或 None = 没有额外资源要收）。
Install = Callable[[], "Disposer | None"]

#: 事件处理器：广播（emit）收 `**payload`；瀑布（waterfall）收 `(value, **context)`。
EventHandler = Callable[..., Any]


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
    """一个插件的状态快照（观测用：台账 / 测试 / 管理面读的就是它）。"""

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
    #: 当前缺的插件级依赖（不阻断生命周期；loader 在装配期据此 fail loud）。
    missing_requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeReport:
    """一次**有效变更**的报告（revision 与它一一对应）。

    retired / activated 是插件名，按发生顺序；同一个插件在一次变更里先退位
    又激活（重启）时，会同时出现在两边。动态依赖回调（`ctx.inject`）用
    `injections_started` / `injections_stopped` 记录（值是 `inject:<序号>`）。
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
    """一条动态依赖订阅的状态快照（观测用：测试 / 管理面 / 排障）。"""

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
    """台账里的一条记录：这个插件的生命周期状态只有一个事实来源，就是它。"""

    name: str
    setup: Any  # SetupFn：Callable[[Context], Disposer | None]
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
    """插件世界的容器与台账。

    用法（装载方视角）：

        ctx = Context()
        ctx.load_plugin(Reporter())     # 依赖未齐也行：先 pending，等依赖
        ctx.load_plugin(Store())        # 依赖齐 → 两者都自动激活
        ctx.unload_plugin("store")      # 提供者走了：依赖它的自动退位（退回 pending）
        ctx.disable_plugin("store")     # 保留登记、只回卷；enable_plugin 一键恢复

    用法（插件作者视角）：

        class MyPlugin:
            name = "my-plugin"
            inject = ("sessions",)      # 必需依赖：缺了先挂起（不报错）
            optional = ("approval",)    # 可选依赖：缺了照常激活

            def setup(self, ctx):
                sessions = ctx.require("sessions")
                ctx.provide("greeting", "你好")     # 注册即回卷：卸载时自动撤销
                ctx.effect("ticker", self._start)    # 嵌套 effect 也行

            def _start(self):
                return lambda: ...                   # 返回自己的撤销动作
    """

    def __init__(self) -> None:
        self._slots: dict[str, Any] = {}
        self._plugins: dict[str, _PluginRecord] = {}
        self._effects: dict[str, _Effect] = {}
        #: 正在装载的 effect 栈（父子关系与"注册即回卷"都靠它）。
        self._stack: list[_Effect] = []
        #: 事件订阅表：事件名 → [(令牌, handler)]；令牌让退订精确到"哪一次订阅"。
        self._handlers: dict[str, list[tuple[int, EventHandler]]] = {}
        self._handler_seq = 0
        #: 变更串行化：可重入（插件在 setup / 清理里装卸别的插件是合法的）。
        self._lock = threading.RLock()
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
        #: 退位级联期间被抑制的插件名：它们刚被停用，但提供者的服务还没撤——
        #: 不抑制的话，级联途中触发的 settle 会看到它们"依赖齐备"又立刻复活。
        self._suppressed: set[str] = set()

    # ------------------------------------------------------------------
    # 变更串行化 / revision
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        """世界版本号：每次**有效变更** +1（失败且没留下痕迹的变更不加）。"""
        with self._lock:
            return self._revision

    @property
    def last_change(self) -> ChangeReport | None:
        """最近一次有效变更的报告（谁退位、谁激活）；还没变过则为 None。"""
        with self._lock:
            return self._last_change

    def _signature(self) -> tuple[Any, ...]:
        """当前世界的"变没变"签名：服务表 + 插件状态 + effect 表 + 动态订阅状态。"""
        return (
            tuple(self._slots),
            tuple((record.name, record.status) for record in self._plugins.values()),
            tuple(self._effects),
            tuple((record.seq, record.status) for record in self._injections.values()),
        )

    @contextmanager
    def _change(self) -> Iterator[None]:
        """把一个公共操作包成"一次变更"：串行化 + 结束时判定 revision。

        嵌套调用（插件 setup 里的 provide / load_plugin …）并入最外层那一次：
        一次用户动作只产生一个 revision。判定用**前后签名对比**——操作报错但
        世界确实变了（例如清理失败）也要 +1；彻底回滚干净的失败则不加。
        """
        with self._lock:
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

    def provide(self, key: str, value: Any) -> None:
        """提供一项服务；同名重复 → DuplicateSlotError（原值原样保留）。

        **注册即回卷**：在某个 effect 的装载期调用时，本次提供自动挂上
        "撤销槽位"的动作——effect 卸载、装载失败，槽位都会被回卷。

        提供之后会把世界推向稳定态：正在等这项服务的插件会当场激活；若某个
        等待者的 setup 抛错，异常会从这次 provide 里抛回来（那个插件标记
        failed，台账里看得到）。

        刻意**不返回**刚提供的值：`ctx.effect("x", lambda: ctx.provide(...))`
        是最常见的写法，返回值会让它变成"install 返回了非撤销函数的怪东西"。
        （`retract` 相反——它把原值交还，那是有用的。）
        """
        if not isinstance(key, str) or not key:
            raise ValueError(f"服务名必须是非空字符串，收到：{key!r}")
        with self._change():
            if key in self._slots:
                raise DuplicateSlotError(
                    f"服务已提供：{key}（重复注册直接报错，不静默覆盖；要换实现先 retract）"
                )
            self._slots[key] = value
            if self._stack:  # 装载期：登记自动回卷（卸载时把它撤掉）
                self._stack[-1].disposers.append(lambda k=key: self._slots.pop(k, None))
            self._settle()

    def retract(self, key: str) -> Any:
        """撤掉一项服务并返回原值；不存在 → MissingServiceError。

        依赖它的活跃插件会随之退位（退回 pending，等服务再出现）。

        注意：**撤销不在回卷范围**——卸载 effect 回卷的是"它提供过的东西"，
        不会把装载期撤掉的槽位再变回来（装载失败是例外：整表还原）。
        所以 setup 里不要 retract 别人的槽（那是拆台，不是自己分内的登记）。
        """
        if key not in self._slots:
            raise MissingServiceError(f"服务不存在：{key}（无法撤掉）")
        with self._change():
            value = self._slots.pop(key)
            self._settle()
            return value

    def get(self, key: str, default: Any = None) -> Any:
        """取一项服务；缺失返回 default（"可选依赖"的取法）。

        注意：default 只在**缺失**时生效——值本身是 None 时与缺失看起来一样；
        要区分"没有"和"有但是 None"请用 require。
        """
        return self._slots.get(key, default)

    def require(self, key: str) -> Any:
        """取一项服务；缺失 → MissingServiceError（"必需依赖"的取法）。"""
        if key not in self._slots:
            raise MissingServiceError(f"服务不存在：{key}（谁提供它？检查装配单）")
        return self._slots[key]

    @property
    def services(self) -> list[str]:
        """当前已提供的服务名（**按提供顺序**的快照：调用方改它不影响容器）。"""
        with self._lock:
            return list(self._slots)

    # ------------------------------------------------------------------
    # effect（注册即回卷）
    # ------------------------------------------------------------------

    def effect(self, name: str, install: Install) -> Disposer:
        """装载一个 effect：执行 install，登记它返回的撤销函数；返回卸载句柄。

        装载期（install 执行中）在 ctx 上做的一切登记——`provide`、嵌套
        `effect`——自动挂进本 effect；调用返回的句柄（或 `unload_effect(name)`）
        会把它们逐层回卷。

        回卷顺序（见 `_unwind`）：**子 effect 先**（同级后进先出），然后自己的
        撤销函数（后进先出）——于是"插件返回的清理动作"运行在它提供的服务被
        撤掉**之前**（清理代码用得着自己的服务）。

        失败即回卷：install 抛错（或返回值不是 None / 可调用）→ 本 effect 及其
        子 effect 全部回卷、名字从台账摘掉，再把原始异常抛出——不留半装。

        install 的返回值是"它自己资源的撤销动作"：若它已经在账本里（例如写成
        `lambda: ctx.on(...)`——on 返回的退订函数早已自动登记），不会重复记账。

        返回的卸载句柄见模块开头的「句柄语义」。

        `name` 以 `plugin:` 开头 → ValueError（那是插件自己的命名空间）。
        """
        with self._change():
            return self._effect(name, install, internal=False)

    def unload_effect(self, name: str) -> None:
        """卸载一个 effect（连同它的子 effect）；不存在 → MissingEffectError。

        某个 disposer 抛错时：**继续回卷其余部分**，最后把第一个异常抛出去
        （卸载不会因为一个清理失败而半途而废——那正是最容易留下垃圾的时刻）。
        """
        if name.startswith(PLUGIN_EFFECT_PREFIX):
            raise ValueError(
                f"插件自己的 effect 不能直接卸载：{name}"
                "（请用 unload_plugin / disable_plugin——它要同步改台账状态）"
            )
        self._unload_named(name)

    @property
    def effects(self) -> list[str]:
        """当前装载的 effect 名（按装载顺序；含插件自己的 `plugin:<名>`）。"""
        with self._lock:
            return list(self._effects)

    # ------------------------------------------------------------------
    # 事件（广播 / 瀑布）
    # ------------------------------------------------------------------

    def on(self, event: str, handler: EventHandler) -> Disposer:
        """订阅一个事件；返回退订函数（幂等）。

        同一个事件可以有多个订阅者，**按注册顺序**派发。两种 handler：

            广播（emit）        handler(**payload)——收到什么看得到；返回值被忽略
            瀑布（waterfall）   handler(value, **context)——返回非 None 即替换当前值

        **注册即回卷**：装载期订阅自动挂上进本 effect 的退订动作——effect 卸载、
        装载失败、插件退位，订阅随之消失（"注册即 effect"在事件上的落地）。

        派发名单在派发开始时取快照：handler 在半途订阅 / 退订，从**下一轮**生效
        （本轮照常跑完）——这样派发过程不会因为订阅表被改而错乱。
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

    def emit(self, event: str, **payload: Any) -> None:
        """广播一个事件：按订阅顺序调用 `handler(**payload)`；返回值忽略。

        handler 抛错**直接往外抛**（fail loud）：总线不替消费方吞错，也不隔离
        订阅者——吞错是消费方自己的策略（真需要"一个断了不影响别人"时，写一个
        替它兜住的 handler，或把那个 subscriber 做成独立 effect）。已经跑过的
        handler 不回滚，后面的 handler 不再跑。
        """
        for _, handler in list(self._handlers.get(event, ())):
            handler(**payload)

    def waterfall(self, event: str, value: Any, **context: Any) -> Any:
        """瀑布事件：每个 handler 可以改写当前值——**中间件链 / 策略插座**。

        handler 签名 `handler(value, **context) -> Any | None`：

            返回 None  不干预（下一个人看到的是原值）
            返回别的   替换当前值（下一个人看到的是它）

        `value` 本身可以是函数——挂载点常常是"把执行动作包一层"的策略
        （超时、重试、截断都这么写），而不是改一个数据字段。

        异常与名单快照语义同 `emit`。
        """
        for _, handler in list(self._handlers.get(event, ())):
            result = handler(value, **context)
            if result is not None:
                value = result
        return value

    @property
    def event_names(self) -> list[str]:
        """当前有订阅者的事件名（订阅者全部退订后不再列出；按首次订阅顺序）。"""
        return [name for name, handlers in self._handlers.items() if handlers]

    # ------------------------------------------------------------------
    # 动态依赖（inject）：依赖齐了才跑一块
    # ------------------------------------------------------------------

    def inject(self, deps: Iterable[str], callback: Callable[[Context], Any]) -> Disposer:
        """注册一块**依赖出现才运行**的代码；返回取消订阅的句柄（幂等）。

        与静态 `inject` 是一对：

            静态 inject = ("store",)   → 缺依赖，**整个插件**不激活
            ctx.inject(["store"], cb)  → 插件本体照常跑，**这一块**等依赖

        回调在依赖齐备时运行，**放进一个子 effect**：它在那里面做的登记
        （provide / on / effect / 返回的清理函数）随"依赖消失"自动回卷；
        依赖再出现时会重跑（这是与 `optional` 的关键区别——optional 是
        "来了不重装"，动态 inject 是"来了就装、走了就拆"）。

        三种典型用途（dsh 里 57 处调用都是这个形状）：

            · 服务出现时往它那里**注册**东西（挂拦截器、加提示词段、注册变量）
            · 服务被换掉后**重新挂**（旧订阅已经随之回卷）
            · 可选能力的延后装配（主体不必等）

        装载期（某个 effect 的 install 里）调用时，**订阅本身**也挂在那个
        effect 下：外层 effect 卸载 → 订阅取消 + 正在生效的回调随之回卷。

        回调抛错 → 记录标 failed、原异常抛给触发方；**不原地重试**（否则每次
        settle 都会再炸一遍）。依赖消失再出现后会重试——错误可查（`injections`）。

        `deps` 必须是非空序列（单个字符串是常见笔误，直接报错）。
        """
        names = self._inject_deps(deps)
        if not callable(callback):
            raise TypeError(f"inject 的 callback 必须可调用，收到：{callback!r}")

        with self._change():
            self._injection_seq += 1
            record = _InjectionRecord(
                seq=self._injection_seq,
                deps=names,
                callback=callback,
                owner_effect=self._stack[-1].name if self._stack else None,
            )
            self._injections[record.seq] = record

            def handle() -> None:
                # 认"这一次注册"（记录身份），不是序号（同句柄语义）。
                with self._lock:
                    if self._injections.get(record.seq) is not record:
                        return
                    # 作为外层 effect 的 disposer 被调用时（unwind 中），只取消
                    # 订阅、不推动稳定态——外层操作收尾时会 settle（与 provide /
                    # on 的自动撤销同一纪律，避免在半个世界里做激活）。
                    with self._change():
                        self._cancel_injection(record)
                    if not self._stack:
                        self._settle()

            if self._stack:  # 装载期：订阅随外层 effect 回卷
                self._stack[-1].disposers.append(handle)
            self._settle()
        return handle

    @property
    def injections(self) -> list[InjectionInfo]:
        """全部动态依赖订阅的状态快照（按注册顺序）。"""
        with self._lock:
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

    def load_plugin(self, plugin: object) -> Disposer:
        """装载一个插件；返回卸载函数（调用即 unload_plugin）。

        返回时插件可能 active（依赖齐备）也可能 pending（依赖缺失，等就绪自动
        激活）——**待激活不是错误**：是否允许残留 pending 是调用方（启动器）的
        判断。同名已存在 → DuplicatePluginError。

        激活失败（setup 抛错）在这里的行为分两种：

        - 本次装载触发的失败 → 异常抛出，**记录不留台账**（不留半装状态）；
        - 别人的操作（如 provide）触发的失败 → 异常也从触发方抛出，但记录
          以 failed 留在台账（错误可查，可用 enable_plugin 重试）。

        返回的卸载句柄见模块开头的「句柄语义」：认"这一次装载"，重复调用 /
        已卸载 / 同名新插件顶上都是安全空操作。
        """
        name = plugin_name(plugin)
        if name in self._plugins:
            raise DuplicatePluginError(
                f"插件已装载：{name}（一个运行时里一个插件名只留一份实例；先 unload_plugin）"
            )
        record = _PluginRecord(
            name=name,
            setup=plugin_setup(plugin),
            inject=plugin_injects(plugin),
            optional=plugin_optionals(plugin),
            requires=plugin_requires(plugin),
        )
        with self._change():
            self._plugins[name] = record
            try:
                self._settle()
            except BaseException:
                if record.status == "failed":
                    self._plugins.pop(name, None)
                raise

        def handle() -> None:
            # 认"这一次装载"（记录身份），不是名字（见模块开头的「句柄语义」）。
            with self._lock:
                if self._plugins.get(name) is record:
                    self.unload_plugin(name)

        return handle

    def unload_plugin(self, name: str) -> None:
        """卸载一个插件：**依赖者先退位**，然后回卷它自己（effect 逐层回卷）。

        退位顺序"深的先退"（间接依赖者先于直接依赖者、提供者最后）：这样每个
        插件跑自己的清理代码时，它依赖的服务都还在。依赖它的活跃插件退回收
        pending——提供者（或别的提供同一服务的插件）回来时，它们会自动重新激活。

        某个插件的清理抛错时：**继续退位与卸载其余部分**，最后把第一个异常抛出去
        （与 effect 回卷同一条纪律：卸载不半途而废——半途而废正是留下垃圾的时刻）。
        """
        record = self._plugins.pop(name, None)
        if record is None:
            raise MissingPluginError(f"插件不存在：{name}（无法卸载）")
        with self._change():
            dependents = list(reversed(self._dependent_records(record)))
            errors: list[BaseException] = []
            self._suppressed.update(dep.name for dep in dependents)
            try:
                for dependent in dependents:
                    self._deactivate_into(dependent, errors)
                self._deactivate_into(record, errors)
            finally:
                for dependent in dependents:
                    self._suppressed.discard(dependent.name)
            self._settle()
        if errors:
            raise errors[0]

    def disable_plugin(self, name: str) -> list[str]:
        """**停用**一个插件：回卷它的 effect、状态标为 disabled，但**保留登记**。

        与卸载的区别（这就是"一键关掉某个能力"要的语义）：

            卸载  → 从台账删掉；要回来必须重新提供插件对象
            停用  → 只回卷、状态 disabled；enable_plugin 用**同一个对象**恢复
                    （setup 会在同一实例上再跑一遍，配置与身份都不丢）

        级联与卸载一致：依赖它的插件一起退位（退回 pending，等它恢复后自动激活）。
        返回被影响的插件名（含自己）。某个插件的清理抛错时：其余照常退位，
        错误最后抛出（同 `unload_plugin`）。
        """
        record = self._plugins.get(name)
        if record is None:
            raise MissingPluginError(f"插件不存在：{name}（无法停用）")
        if record.status == "disabled":
            raise ValueError(f"插件已处于停用状态：{name}")
        dependents = list(reversed(self._dependent_records(record)))
        with self._change():
            # 先改自己的状态：回卷途中若触发 settle，它不能把自己当作 pending 复活。
            record.status = "disabled"
            self._suppressed.update(dep.name for dep in dependents)
            errors: list[BaseException] = []
            try:
                for dependent in dependents:
                    self._deactivate_into(dependent, errors)
                self._deactivate_into(record, errors, status="disabled")
            finally:
                for dependent in dependents:
                    self._suppressed.discard(dependent.name)
            self._settle()
        if errors:
            raise errors[0]
        return [record.name, *(dep.name for dep in dependents)]

    def enable_plugin(self, name: str) -> None:
        """重新启用被停用（或激活失败）的插件：标回 pending，交给 settle 定夺。

        **不保证一定激活**：依赖缺失时停在 pending（台账里看得见缺什么）——
        这是"启用"该有的诚实行为，不是"点了就应该活"。
        """
        record = self._plugins.get(name)
        if record is None:
            raise MissingPluginError(f"插件不存在：{name}（无法启用）")
        if record.status not in ("disabled", "failed"):
            raise ValueError(
                f"插件当前是 {record.status}，不需要启用（只有 disabled / failed 可以）"
            )
        with self._change():
            record.status = "pending"
            record.error = None
            self._settle()

    @property
    def plugins(self) -> list[PluginInfo]:
        """全部插件的状态快照（按装载顺序）。"""
        with self._lock:
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
        """插件级依赖里当前不在台账的那些（供 loader / 管理面做装配期校验）。"""
        names = list(self._plugins)
        return tuple(
            required
            for required in record.requires
            if not any(matches_plugin_requirement(required, name) for name in names)
        )

    @property
    def active_plugins(self) -> list[str]:
        """运行中的插件名（按装载顺序）。"""
        with self._lock:
            return [rec.name for rec in self._plugins.values() if rec.status == "active"]

    @property
    def pending_plugins(self) -> list[str]:
        """等待依赖的插件名（按装载顺序）——启动器检查残留用的就是它。"""
        with self._lock:
            return [rec.name for rec in self._plugins.values() if rec.status == "pending"]

    @property
    def disabled_plugins(self) -> list[str]:
        """被停用的插件名（按装载顺序）。"""
        with self._lock:
            return [rec.name for rec in self._plugins.values() if rec.status == "disabled"]

    def missing_dependencies(self, name: str) -> list[str]:
        """某个插件当前缺失的必需依赖（用于 fail-loud 文案 / 观测）。"""
        record = self._plugins.get(name)
        if record is None:
            raise MissingPluginError(f"插件不存在：{name}")
        return [dep for dep in record.inject if dep not in self._slots]

    # ------------------------------------------------------------------
    # 内部：effect 装载 / 回卷
    # ------------------------------------------------------------------

    def _effect(self, name: str, install: Install, *, internal: bool) -> Disposer:
        """`effect` / 插件装载共用的实现（internal 放行 `plugin:` 前缀）。"""
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
            result = install()
            if result is not None and not callable(result):
                raise TypeError(
                    f"effect {name!r} 的 install 必须返回 None 或撤销函数，收到：{result!r}"
                )
        except BaseException:
            # 装载失败：把已登记的（含子 effect）撤销掉再抛——不留半装状态。
            self._stack.pop()
            self._detach(record)
            self._forget(record)
            self._unwind(record)  # 回卷途中的异常不遮蔽原始异常
            raise
        self._stack.pop()
        if result is not None and not any(result is item for item in record.disposers):
            # install 的返回值 = "它自己资源的撤销动作"。若已在账本里（如
            # `lambda: ctx.on(...)` 返回的退订——on 早已自动登记），不重复记账。
            record.disposers.append(result)
        self._settle()  # 本次装载可能解开了别的插件的依赖（重入时自动跳过）

        def handle() -> None:
            # 认"这一次装载"（记录身份），不是名字（见模块开头的「句柄语义」）。
            if self._effects.get(name) is record:
                self._unload_named(name)

        return handle

    def _unload_named(self, name: str) -> None:
        """按名字回卷 + 稳定化 + 重抛第一个清理异常（公共入口与句柄共用）。"""
        with self._change():
            errors = self._unload_effect(name)
            self._settle()
        if errors:
            raise errors[0]

    def _unload_effect(self, name: str) -> list[BaseException]:
        """按名字回卷一个 effect；返回途中的异常（不回卷=不管）。"""
        record = self._effects.get(name)
        if record is None:
            raise MissingEffectError(f"effect 不存在：{name}（无法卸载）")
        self._detach(record)  # 先与父链断开：父之后卸载时不会重复回卷它
        self._forget(record)  # 从台账摘掉整棵子树（不动 children——回卷要用）
        return self._unwind(record)

    @staticmethod
    def _detach(record: _Effect) -> None:
        """把 effect 从父的 children 列表里摘掉（防重复回卷）。"""
        if record.parent is not None and record in record.parent.children:
            record.parent.children.remove(record)

    def _forget(self, record: _Effect) -> None:
        """从名字台账里摘掉一个 effect（连同子树）；不碰 children 结构。"""
        self._effects.pop(record.name, None)
        for child in record.children:
            self._forget(child)

    def _unwind(self, record: _Effect) -> list[BaseException]:
        """回卷：子 effect 先（同级后进先出），然后自己的撤销动作（后进先出）。

        返回途中的异常，**不错过其余回卷**（某个清理炸了不代表剩下的不清理）。
        """
        errors: list[BaseException] = []
        while record.children:
            child = record.children.pop()  # 同级后进先出
            errors.extend(self._unwind(child))
        while record.disposers:
            disposer = record.disposers.pop()  # 后进先出
            try:
                disposer()
            except BaseException as exc:  # 全部捕获、统一重抛
                errors.append(exc)
        return errors

    # ------------------------------------------------------------------
    # 内部：激活 / 退位 / 稳定化
    # ------------------------------------------------------------------

    def _deps_ok(self, record: _PluginRecord) -> bool:
        return all(dep in self._slots for dep in record.inject)

    @staticmethod
    def _inject_deps(deps: Iterable[str]) -> tuple[str, ...]:
        """校验并规范化动态依赖声明（与静态 inject 同一套 fail-loud 纪律）。"""
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

    def _start_injection(self, record: _InjectionRecord) -> None:
        """依赖齐了：把回调放进一个子 effect 里运行（注册的一切随它回卷）。"""
        effect_name = f"{INJECT_EFFECT_PREFIX}{record.seq}"
        record.status = "active"
        record.error = None
        try:
            self._effect(effect_name, lambda: record.callback(self), internal=True)
        except BaseException as exc:
            # 回调抛错：标 failed、不原地重试（否则每次 settle 都要再炸一遍）。
            record.status = "failed"
            record.error = exc
            record.effect = None
            raise
        record.effect = effect_name
        self._report_injections_started.append(effect_name)

    def _stop_injection(self, record: _InjectionRecord) -> None:
        """依赖不再齐：回卷回调的 effect，**订阅保留**（依赖回来时重跑）。"""
        effect_name, record.effect = record.effect, None
        record.status = "pending"
        record.error = None
        if effect_name is not None:
            self._report_injections_stopped.append(effect_name)
            errors = self._unload_effect(effect_name)
            if errors:
                raise errors[0]  # 清理失败 fail loud（其余回卷照常完成）

    def _cancel_injection(self, record: _InjectionRecord) -> None:
        """取消订阅本身：回卷回调的 effect、从订阅表摘掉。"""
        self._injections.pop(record.seq, None)
        self._stop_injection(record)

    def _activate(self, record: _PluginRecord) -> None:
        """激活：把 setup 放进一个 effect 里跑（`plugin:<名>`）。

        装载期做的一切登记都挂在这个 effect 下——退位时逐层回卷（插件自己的
        清理动作先跑，它提供的服务随后撤）。失败标记 failed、**服务表还原**
        （等于"什么都没发生"），异常抛出。
        """
        before = dict(self._slots)
        effect_name = f"{PLUGIN_EFFECT_PREFIX}{record.name}"
        try:
            self._effect(effect_name, lambda: record.setup(self), internal=True)
        except BaseException as exc:
            record.status = "failed"
            record.error = exc
            self._slots = before  # 保险：装载失败 = 服务表回到装载前
            raise
        record.effect = effect_name
        # 本插件在 active 期间新提供的服务（按提供顺序）——退位级联按它扩散。
        record.provided = tuple(key for key in self._slots if key not in before)
        record.status = "active"
        record.error = None
        self._report_activated.append(record.name)

    def _deactivate(self, record: _PluginRecord, *, status: PluginStatus = "pending") -> None:
        """回卷一个插件：卸载它的 effect（子先父后、服务自动撤销）。

        只改状态与表，**不推送稳定态**——调用方（settle 循环 / 卸载级联）负责
        在恰当的时候收尾（半拆状态下 settle 会看到错误的中间世界）。
        """
        record.status = status
        record.error = None
        record.provided = ()
        effect_name, record.effect = record.effect, None
        if effect_name is not None:
            self._report_retired.append(record.name)
            errors = self._unload_effect(effect_name)
            if errors:
                raise errors[0]  # 清理失败 fail loud（其余回卷照常完成）

    def _deactivate_into(
        self,
        record: _PluginRecord,
        errors: list[BaseException],
        *,
        status: PluginStatus = "pending",
    ) -> None:
        """退位一个插件，把清理异常收进 `errors`——用于**级联**（继续处理其余部分）。

        单个插件的入口（`_deactivate`）抛第一个异常；批量退位（一次卸载里的
        提供者 + 一串依赖者）要"不半途而废"：全部退完，异常最后一起报。
        """
        try:
            self._deactivate(record, status=status)
        except BaseException as exc:
            errors.append(exc)

    def _dependent_records(self, provider: _PluginRecord) -> list[_PluginRecord]:
        """列出需要随 provider 一起退位的活跃插件（**不含 provider 自己**）。

        顺序 = "由近及远"（先直接依赖者、再它们的依赖者）；调用方反转即得
        "深的先退"。服务闭包按层扩展，天然容忍环形依赖（环在同一批被选中，
        不会无限循环）。
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

        触发时机：provide / retract / load_plugin / unload_plugin / disable /
        enable / effect 装载与卸载。

        两种跳过（都为了让激活永远发生在**干净的顶层**）：

            已在稳定化中  最外层的循环会重新扫描，直到不再变化
            装载期（栈非空）  本次 effect 的收尾会再来一趟（否则插件会激活在
                          别人的 effect 里，被那个 effect 的卸载牵连）

        激活失败（setup 抛错）会把异常抛给触发方：世界停在失败点，那条记录以
        failed 留着（错误可查、可重试）。这是有意的 fail loud。
        """
        if self._settling or self._stack:
            return
        self._settling = True
        try:
            while True:
                changed = False
                # 动态依赖：先停"依赖不再满足"的（订阅保留），再在最后启动"依赖已齐"的。
                for injection in list(self._injections.values()):
                    deps_ok = all(dep in self._slots for dep in injection.deps)
                    if injection.status == "active" and not deps_ok:
                        self._stop_injection(injection)
                        changed = True
                    elif injection.status == "failed" and not deps_ok:
                        # 依赖走了：回到 pending，等它再出现时重试（不原地空转）。
                        injection.status = "pending"
                        injection.error = None
                        changed = True
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
                for injection in list(self._injections.values()):
                    if injection.status == "pending" and all(
                        dep in self._slots for dep in injection.deps
                    ):
                        self._start_injection(injection)
                        changed = True
                if not changed:
                    return
        finally:
            self._settling = False
