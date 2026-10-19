"""agent —— Agent 句柄与注册表（基础插件）。

提供 `agent` 服务：谁在跑、怎么给它发消息、怎么停。

    registry.create(options=..., session_id=..., agent_id=...) -> AgentHandle
    registry.resume(session_id, options=...)                  -> AgentHandle
    registry.get(id) / list() / roots()

句柄的五个输入语义（与 dsh 一致）：

    followup(text)   排队一条下一轮输入并唤醒驱动器（空闲时同步跑完，返回结果）
    steer(text)      中途引导：落在**下一个 step 边界**，不新开一轮
    inject(text)     注入面向模型的上下文，**不唤醒**驱动器
    cancel()         请求中止当前轮（协作式：step 边界与流分块之间检查）
    when_idle()      等到所有排队输入都跑完

**注册表是惰性的**：没有驱动器（`agent-loop`）注册工厂之前，`create()` 会
`AgentNotAvailable`——"谁来跑"是可替换的，agent 包自己不创建模型请求。

`agent/*` 事件（观察与拦截）：

    agent/created  agent/disposed  agent/status
    agent/turn-start  agent/turn-end
    agent/step-start  agent/step-end
    agent/delta（流式文本）  agent/tool-call  agent/tool-result
    agent/request（waterfall，可在分发前改写请求）  agent/request-error
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from core import Context, Disposer

#: agent 服务的名字。
AGENT_SERVICE = "agent"

#: 事件名。
AGENT_CREATED = "agent/created"
AGENT_DISPOSED = "agent/disposed"
AGENT_STATUS = "agent/status"
AGENT_TURN_START = "agent/turn-start"
AGENT_TURN_END = "agent/turn-end"
AGENT_STEP_START = "agent/step-start"
AGENT_STEP_END = "agent/step-end"
AGENT_DELTA = "agent/delta"
AGENT_TOOL_CALL = "agent/tool-call"
AGENT_TOOL_RESULT = "agent/tool-result"
AGENT_REQUEST = "agent/request"
AGENT_REQUEST_ERROR = "agent/request-error"


class AgentError(RuntimeError):
    """agent 相关的错误基类。"""


class AgentNotAvailable(AgentError):
    """没有驱动器注册（没装 agent-loop，或它还没激活）。"""


class UnknownAgentError(AgentError):
    """agent id 不存在。"""


class AgentFactory(Protocol):
    """驱动器：由 `agent-loop` 注册，负责真正创建 / 恢复 agent。"""

    def create(
        self, options: AgentOptions, *, session_id: str | None, agent_id: str | None, parent: str | None
    ) -> AgentHandle:
        ...

    def resume(
        self, session_id: str, options: AgentOptions, *, agent_id: str | None, parent: str | None
    ) -> AgentHandle:
        ...


@dataclass
class _Inbox:
    """待处理输入：下一轮 / 下一步。"""

    next_turn: list[str] = field(default_factory=list)
    next_step: list[str] = field(default_factory=list)


class AgentHandle:
    """一个 agent 的句柄：发消息、取消、等空闲、释放。

    句柄**不实现循环**：它把调用转给驱动器注入的 `backend`。所以换驱动器
    不需要改这里。
    """

    def __init__(
        self,
        agent_id: str,
        options: AgentOptions,
        backend: AgentBackend,
        *,
        parent: str | None = None,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self.id = agent_id
        self.options = options
        self.parent = parent
        self._backend = backend
        self._emit = emit or (lambda *a, **k: None)
        self._status: AgentStatus = "idle"
        self._disposed = False
        self._inbox = _Inbox()
        self._lock = threading.RLock()
        self._busy = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._cancelled = threading.Event()
        self._open = True
        #: 由注册表注入：dispose 后把自己的 id 摘掉。
        self._on_dispose: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._backend.session_id

    def followup(self, text: str) -> TurnResult | None:
        """排队一条下一轮输入；空闲时同步跑完并返回结果。

        正在跑（别的线程）时只入队并返回 None——`when_idle()` 可以等它跑完。
        """
        self._assert_open()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("followup 的文本不能为空")
        with self._lock:
            self._inbox.next_turn.append(text)
            if self._busy.is_set():
                return None
            self._busy.set()
            self._idle.clear()
            self._status = "running"
            self._emit(AGENT_STATUS, agent=self, status="running")
        result: TurnResult | None = None
        try:
            result = self._drain()
        finally:
            with self._lock:
                self._busy.clear()
                self._idle.set()
                if not self._disposed:
                    self._status = "idle"
            self._emit(AGENT_STATUS, agent=self, status=self._status)
        return result

    def steer(self, text: str) -> None:
        """中途引导：下一个 step 边界生效（不新开一轮）。"""
        self._assert_open()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("steer 的文本不能为空")
        with self._lock:
            self._inbox.next_step.append(text)
        self._backend.steer(text)

    def inject(self, text: str) -> None:
        """注入上下文：不唤醒驱动器（空闲时留到下一轮）。"""
        self._assert_open()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("inject 的文本不能为空")
        with self._lock:
            self._inbox.next_step.append(text)
        self._backend.inject(text)

    def cancel(self) -> bool:
        """请求中止当前轮；没有在跑 → False。"""
        with self._lock:
            running = self._busy.is_set()
            self._cancelled.set()
        if not running:
            return False
        return self._backend.cancel()

    def when_idle(self, timeout: float | None = None) -> bool:
        """等待所有排队输入跑完；返回是否在超时前空闲。"""
        return self._idle.wait(timeout)

    def dispose(self) -> None:
        """停掉这个 agent：取消当前轮、调用驱动器清理、从注册表摘除。"""
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
            self._open = False
            self._status = "disposed"
        self._backend.cancel()
        self._backend.dispose()
        if self._on_dispose is not None:
            self._on_dispose(self.id)
        self._emit(AGENT_DISPOSED, agent=self)
        self._emit(AGENT_STATUS, agent=self, status="disposed")
        self._idle.set()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def status(self) -> AgentStatus:
        return self._status

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "status": self._status,
            "mode": self.options.mode,
            "provider": self.options.provider,
            "model": self.options.model,
            "parent": self.parent,
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _drain(self) -> TurnResult | None:
        """把 next_turn 里的输入依次跑完（调用方已置 busy）。"""
        last: TurnResult | None = None
        while True:
            with self._lock:
                if not self._inbox.next_turn:
                    return last
                text = self._inbox.next_turn.pop(0)
                self._cancelled.clear()
            last = self._backend.run_turn(text)
            if last.status == "cancelled":
                return last

    def _assert_open(self) -> None:
        if not self._open:
            raise AgentError(f"agent 已释放：{self.id}")


class AgentRegistry:
    """agent 注册表：创建 / 恢复 / 查找 / 列举 + 驱动器注册。

    线程安全：创建与查找在一把短锁里；跑 turn 不持这把锁（句柄自己管）。
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._factory: AgentFactory | None = None
        self._agents: dict[str, AgentHandle] = {}
        self._seq = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 驱动器
    # ------------------------------------------------------------------

    def register_factory(self, factory: AgentFactory) -> Disposer:
        """注册驱动器（由 agent-loop 调用）；返回注销句柄。"""
        for name in ("create", "resume"):
            if not callable(getattr(factory, name, None)):
                raise TypeError(f"驱动器必须实现 {name}()：{factory!r}")
        if self._factory is not None:
            raise AgentError("已经有驱动器注册了（先注销旧的）")
        self._factory = factory

        def unregister() -> None:
            if self._factory is factory:
                self._factory = None

        return unregister

    @property
    def has_driver(self) -> bool:
        return self._factory is not None

    # ------------------------------------------------------------------
    # 创建 / 恢复
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        options: AgentOptions | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        parent: str | None = None,
    ) -> AgentHandle:
        factory = self._require_factory()
        with self._lock:
            resolved_id = agent_id or self._next_id()
            if resolved_id in self._agents:
                raise AgentError(f"agent 已存在：{resolved_id}")
        handle = factory.create(
            options or AgentOptions(), session_id=session_id, agent_id=resolved_id, parent=parent
        )
        handle._emit = self._ctx.emit  # type: ignore[attr-defined]
        handle._on_dispose = self.remove
        with self._lock:
            if resolved_id in self._agents:
                handle.dispose()
                raise AgentError(f"agent 已存在：{resolved_id}")
            self._agents[resolved_id] = handle
        self._ctx.emit(AGENT_CREATED, agent=handle, options=handle.options)
        return handle

    def resume(
        self,
        session_id: str,
        *,
        options: AgentOptions | None = None,
        agent_id: str | None = None,
        parent: str | None = None,
    ) -> AgentHandle:
        factory = self._require_factory()
        with self._lock:
            resolved_id = agent_id or self._next_id()
        handle = factory.resume(
            session_id, options or AgentOptions(), agent_id=resolved_id, parent=parent
        )
        handle._emit = self._ctx.emit  # type: ignore[attr-defined]
        handle._on_dispose = self.remove
        with self._lock:
            self._agents[resolved_id] = handle
        self._ctx.emit(AGENT_CREATED, agent=handle, options=handle.options)
        return handle

    def make_handle(
        self,
        agent_id: str,
        options: Any,
        *,
        backend: Any,
        parent: str | None = None,
    ) -> AgentHandle:
        """驱动器用：把它的后端包成句柄（**不要求驱动器 import 本模块**）。

        后端只需要四个可调用属性 + session_id：
        `run_turn(text)` / `steer(text)` / `inject(text)` / `cancel()` / `dispose()`。
        """
        for attr in ("session_id", "run_turn", "steer", "inject", "cancel", "dispose"):
            if not hasattr(backend, attr):
                raise TypeError(f"驱动器后端缺少 {attr}：{backend!r}")
        handle = AgentHandle(agent_id, options, backend, parent=parent)
        handle._emit = self._ctx.emit  # type: ignore[attr-defined]
        handle._on_dispose = self.remove  # type: ignore[attr-defined]
        return handle

    def remove(self, agent_id: str) -> None:
        """从注册表摘除（dispose 时由句柄调用）。"""
        with self._lock:
            self._agents.pop(agent_id, None)

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------

    def get(self, agent_id: str) -> AgentHandle | None:
        with self._lock:
            return self._agents.get(agent_id)

    def require(self, agent_id: str) -> AgentHandle:
        handle = self.get(agent_id)
        if handle is None:
            known = "、".join(self._agents) or "（没有 agent）"
            raise UnknownAgentError(f"agent 不存在：{agent_id!r}（当前：{known}）")
        return handle

    def list(self) -> list[AgentHandle]:
        with self._lock:
            return list(self._agents.values())

    def roots(self) -> list[AgentHandle]:
        """没有 parent 的 agent。"""
        return [handle for handle in self.list() if handle.parent is None]

    def children(self, agent_id: str) -> list[AgentHandle]:
        return [handle for handle in self.list() if handle.parent == agent_id]

    def snapshot(self) -> list[dict[str, Any]]:
        return [handle.info() for handle in self.list()]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        while True:
            self._seq += 1
            candidate = f"agent-{self._seq}"
            if candidate not in self._agents:
                return candidate

    def _require_factory(self) -> AgentFactory:
        if self._factory is None:
            raise AgentNotAvailable(
                "没有 agent 驱动器（装上 agent-loop 插件，或手动 register_factory）"
            )
        return self._factory


class AgentPlugin:
    """提供 `agent` 服务（注册表）。装配单里应声明 `role="base"`。

    **它自己不是完整的**：没有驱动器（`agent-loop`）时 `create()` 会抛
    `AgentNotAvailable`。所以这里声明插件级依赖——与 `llm` 要求
    `llm-provider` 同构：接缝在 base，填接缝的实现是 capability。
    """

    name = "agent"
    #: 至少一个驱动器在场（`agent-loop` 的实例都算）。
    requires = ("agent-loop",)

    def setup(self, ctx: Context) -> None:
        ctx.provide(AGENT_SERVICE, AgentRegistry(ctx))


def agent_plugin(**config: Any) -> AgentPlugin:
    """工厂（当前无配置）。"""
    return AgentPlugin(**config)


__all__ = [
    "AGENT_CREATED",
    "AGENT_DELTA",
    "AGENT_DISPOSED",
    "AGENT_REQUEST",
    "AGENT_REQUEST_ERROR",
    "AGENT_SERVICE",
    "AGENT_STATUS",
    "AGENT_STEP_END",
    "AGENT_STEP_START",
    "AGENT_TOOL_CALL",
    "AGENT_TOOL_RESULT",
    "AGENT_TURN_END",
    "AGENT_TURN_START",
    "AgentError",
    "AgentFactory",
    "AgentHandle",
    "AgentNotAvailable",
    "AgentPlugin",
    "AgentRegistry",
    "UnknownAgentError",
    "agent_plugin",
]


# ======================================================================
# 以下合并自 components/agent/types.py
# ======================================================================


#: 一轮的结束状态。
TurnStatus = Literal["done", "max_steps", "budget_exceeded", "cancelled", "error"]

#: agent 的对外状态。
AgentStatus = Literal["idle", "running", "disposed", "failed"]


@dataclass(frozen=True)
class AgentOptions:
    """创建一个 agent 时的配置（模式就是它的预设）。

    全部可选：没给的路由用会话记录里的选择，再退到装配默认（0044）。
    """

    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    token_budget: int | None = None
    #: 模式名（agent-admin 用来分组 / 展示；驱动器不解释它）。
    mode: str | None = None
    #: 驱动器私有的附加选项（不参与通用校验）。
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError(f"max_steps 必须是 None 或 >= 1，收到：{self.max_steps}")
        if self.token_budget is not None and self.token_budget < 1:
            raise ValueError(f"token_budget 必须是 None 或 >= 1，收到：{self.token_budget}")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError(f"max_tokens 必须是 None 或 >= 1，收到：{self.max_tokens}")

    def merged(self, **overrides: Any) -> AgentOptions:
        """以自己为底，覆盖非 None 的字段（模式 → 显式参数的合并）.。"""
        data = {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "max_steps": self.max_steps,
            "token_budget": self.token_budget,
            "mode": self.mode,
            "extra": dict(self.extra),
        }
        for key, value in overrides.items():
            if value is not None:
                data[key] = value
        return AgentOptions(**data)


@dataclass(frozen=True)
class TurnResult:
    """一轮的结果（驱动器产出，句柄转给调用方）。"""

    turn: int
    status: TurnStatus
    final_text: str = ""
    steps: int = 0
    #: 用量（鸭子类型：由驱动器构造，本模块只搬运）。
    usage: Any = None
    error: str | None = None


@dataclass(frozen=True)
class AgentBackend:
    """驱动器提供给句柄的能力（全部可调用，句柄只转发）。"""

    session_id: str
    run_turn: Callable[[str], TurnResult]
    steer: Callable[[str], None]
    inject: Callable[[str], None]
    cancel: Callable[[], bool]
    dispose: Callable[[], None]


__all__ = [
    "AgentBackend",
    "AgentOptions",
    "AgentStatus",
    "TurnResult",
    "TurnStatus",
]
