"""agent-loop —— 默认驱动器（能力插件）：模型请求 → 工具执行 → 重复。

它向 `agent` 注册一个工厂：agent 包管"谁在跑、怎么发消息"，本插件管
"一个 turn 怎么跑"。换一个驱动器（比如"一步就收尾"的假循环）只要注册
同一个工厂形状，agent 包一行不用改。

**一轮的流程**：

    turn/start（user 消息）
      └─ step 1..n
           渲染提示词（指纹变了才写 system/message）
           llm.stream（工具表来自 tool-admin；事件 agent/request 可改写请求）
           写 assistant（文本 + tool_calls）
           有工具调用 → 走 tool-admin 管线 → 写 tool/result → 下一步
           没有       → turn/end(done)
      └─ steer / inject 在 step 边界落成 user/message（不新开一轮）

**取消**是协作式的：每块流、每个 step 边界检查一次；已经吐出的文本保留。
**预算**：每个 step 后累计 usage，超了以 `budget_exceeded` 收尾。

插件之间只靠**服务名 / 事件名 / 数据形状**协作：本模块不 import 其它插件
（决策 0052 要求单向、无环；这里是零依赖）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core import Context

# ---------------------------------------------------------------------------
# 服务名 / 事件名（字符串约定；每个插件各存一份）
# ---------------------------------------------------------------------------

AGENT_SERVICE = "agent"
AGENT_DELTA = "agent/delta"
AGENT_REQUEST = "agent/request"
AGENT_REQUEST_ERROR = "agent/request-error"
AGENT_STEP_END = "agent/step-end"
AGENT_STEP_START = "agent/step-start"
AGENT_TOOL_CALL = "agent/tool-call"
AGENT_TOOL_RESULT = "agent/tool-result"
AGENT_TURN_END = "agent/turn-end"
AGENT_TURN_START = "agent/turn-start"
LLM_SERVICE = "llm"
PROMPT_SERVICE = "prompt"
SESSIONS_SERVICE = "sessions"
TOOL_ADMIN_SERVICE = "tool-admin"
WORKSPACE_SERVICE = "workspace"

ASSISTANT = "assistant"
SESSION_START = "session/start"
STEP_END = "step/end"
STEP_START = "step/start"
SYSTEM_MESSAGE = "system/message"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
TURN_END = "turn/end"
TURN_START = "turn/start"
USER_MESSAGE = "user/message"


# ---------------------------------------------------------------------------
# 本地数据类型（与 llm / agent 插件的同名类型字段一致；刻意各存一份）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finish:
    reason: str = "stop"
    usage: Any = None
    error: str | None = None
    error_code: str | None = None
    type: str = "finish"


@dataclass(frozen=True)
class TextDelta:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolCallsDelta:
    deltas: tuple[Mapping[str, Any], ...]
    type: str = "tool_calls"


@dataclass(frozen=True)
class TurnResult:
    turn: int
    status: str
    final_text: str = ""
    steps: int = 0
    usage: Any = None
    error: str | None = None


@dataclass(frozen=True)
class AgentOptions:
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    token_budget: int | None = None
    mode: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def merged(self, **overrides: Any) -> AgentOptions:
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
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"工具 {self.name!r} 的参数不是合法 JSON：{self.arguments!r}（{exc}）"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"工具 {self.name!r} 的参数必须是 JSON 对象")
        return value


class ToolCallAccumulator:
    """按 index 拼接流式工具调用分片（与 llm 插件的同名类同形）。"""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, deltas: Iterable[Mapping[str, Any]]) -> None:
        for delta in deltas:
            index = int(delta.get("index", 0))
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if delta.get("id"):
                slot["id"] = str(delta["id"])
            function = delta.get("function")
            if isinstance(function, Mapping):
                if function.get("name"):
                    slot["name"] = str(function["name"])
                if function.get("arguments"):
                    slot["arguments"] += str(function["arguments"])

    def calls(self) -> tuple[ToolCall, ...]:
        out = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            if not slot["name"]:
                continue
            out.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"] or "{}",
                )
            )
        return tuple(out)

    def __bool__(self) -> bool:
        return bool(self._calls)


@dataclass(frozen=True)
class _Backend:
    """驱动器的后端形状（agent 插件按属性读取，不 import）。"""

    session_id: str
    run_turn: Any
    steer: Any
    inject: Any
    cancel: Any
    dispose: Any


class AgentLoopPlugin:
    """提供驱动器：注册工厂到 `agent` 服务。装配单里应声明 `role="capability"`。"""

    name = "agent-loop"
    inject = (
        AGENT_SERVICE,
        LLM_SERVICE,
        SESSIONS_SERVICE,
        PROMPT_SERVICE,
        TOOL_ADMIN_SERVICE,
    )
    #: 工作区可选：有就作为提示词里的 cwd 与工具的默认目录。
    optional = (WORKSPACE_SERVICE,)

    #: 没显式给 max_steps 时的**安全上限**（防止模型反复调工具跑飞）。
    #: 装配单里显式给 `default_max_steps: null` 可以关掉它（改成不限）。
    SAFETY_MAX_STEPS = 64

    def __init__(
        self,
        *,
        default_max_steps: int | None = SAFETY_MAX_STEPS,
        default_token_budget: int | None = None,
    ) -> None:
        self._default_max_steps = default_max_steps
        self._default_token_budget = default_token_budget

    def setup(self, ctx: Context):
        driver = LoopDriver(
            ctx,
            default_max_steps=self._default_max_steps,
            default_token_budget=self._default_token_budget,
        )
        registry = ctx.require(AGENT_SERVICE)
        return registry.register_factory(driver)


class LoopDriver:
    """创建 / 恢复 agent；每个 agent 一个 `_AgentRunner`。"""

    def __init__(
        self,
        ctx: Context,
        *,
        default_max_steps: int | None,
        default_token_budget: int | None,
    ) -> None:
        self._ctx = ctx
        self._default_max_steps = default_max_steps
        self._default_token_budget = default_token_budget

    def create(
        self,
        options: AgentOptions,
        *,
        session_id: str | None,
        agent_id: str | None,
        parent: str | None,
    ) -> Any:
        resolved_id = agent_id or "agent"
        session = session_id or f"{resolved_id}-session"
        runner = _AgentRunner(
            self._ctx,
            resolved_id,
            options,
            session,
            default_max_steps=self._default_max_steps,
            default_token_budget=self._default_token_budget,
        )
        runner.open_session()
        return runner.handle(parent=parent)

    def resume(
        self,
        session_id: str,
        options: AgentOptions,
        *,
        agent_id: str | None,
        parent: str | None,
    ) -> Any:
        resolved_id = agent_id or "agent"
        runner = _AgentRunner(
            self._ctx,
            resolved_id,
            options,
            session_id,
            default_max_steps=self._default_max_steps,
            default_token_budget=self._default_token_budget,
        )
        runner.open_session(resume=True)
        return runner.handle(parent=parent)


class _AgentRunner:
    """一个 agent 的状态与循环（每个句柄一个）。"""

    def __init__(
        self,
        ctx: Context,
        agent_id: str,
        options: AgentOptions,
        session_id: str,
        *,
        default_max_steps: int | None = None,
        default_token_budget: int | None = None,
    ) -> None:
        self._ctx = ctx
        self._agent_id = agent_id
        self._options = options
        self._session_id = session_id
        self._default_max_steps = default_max_steps
        self._default_token_budget = default_token_budget
        self._cancel = threading.Event()
        self._pending_steps: list[tuple[str, str]] = []  # (text, source)
        self._lock = threading.RLock()
        self._last_prompt_fingerprint: str | None = None

    # ------------------------------------------------------------------
    # 会话 / 路由
    # ------------------------------------------------------------------

    def open_session(self, *, resume: bool = False) -> None:
        sessions = self._ctx.require(SESSIONS_SERVICE)
        rendered = self._render_prompt()
        route = self._route()
        sessions.open(
            self._session_id,
            system_prompt=rendered.text,
            prompt_trace=self._trace(rendered),
            model={
                "provider": route[0],
                "model": route[1],
                "mode": self._options.mode,
            },
        )
        # 恢复的会话历史里已有自己的 system/message；把当前指纹当作基线，
        # 避免打开时无条件再写一条。提示词真变了，第一个 turn 会补写。
        del resume
        self._last_prompt_fingerprint = rendered.fingerprint

    def _route(self) -> tuple[str, str]:
        """显式选项 > 会话记录 > 装配默认（0044）。

        会话记录优先是刻意的：恢复一条会话时应该用它**自己**的模型，而不是
        启动时的默认值。但记录里的路由可能在当前装配里不存在（比如离线会话
        用 `fake` 建的，现在只装了 `deepseek`）——这时**不静默换模型**，而是
        给出可操作的错误：换会话、显式指定模型、或删掉旧会话日志。
        """
        llm = self._ctx.require(LLM_SERVICE)
        provider = self._options.provider
        model = self._options.model
        if provider and model:
            return self._check_route(provider, model, source="创建参数")

        recorded: dict[str, Any] = {}
        sessions = self._ctx.require(SESSIONS_SERVICE)
        session = sessions.get(self._session_id)
        if session is not None:
            for event in session.events:
                if event.kind == SESSION_START:
                    recorded = dict(event.payload.get("model") or {})
                    break
        provider = provider or recorded.get("provider")
        model = model or recorded.get("model")
        if provider and model:
            source = "会话记录" if recorded.get("provider") == provider else "装配默认"
            return self._check_route(provider, model, source=source)

        default = llm.default_route
        if default is None:
            raise RuntimeError(
                "agent 没有可用的模型路由（选项、会话记录、装配默认都没有）"
            )
        return self._check_route(default[0], default[1], source="装配默认")

    def _check_route(self, provider: str, model: str, *, source: str) -> tuple[str, str]:
        """路由必须在当前装配里可用；不可用时给可操作的错误（不静默换模型）。"""
        llm = self._ctx.require(LLM_SERVICE)
        available = llm.list_providers()
        if provider not in available:
            hint = ""
            if source == "会话记录":
                hint = (
                    f"\n这条会话（{self._session_id}）是用 {provider!r} 建立的，"
                    f"当前装配只注册了 {list(available)}。"
                    "\n处理方式（任选其一）："
                    "\n  · 换一个会话：--session <新 id>"
                    "\n  · 显式指定模型：--provider <路由> --model <模型>"
                    f"\n  · 删除旧会话日志：sessions/{self._session_id}.jsonl"
                    "（或对应 --<cwd>--/ 下的会话目录）"
                )
            raise RuntimeError(
                f"模型路由不可用：{provider!r}（来源：{source}）"
                f"；当前已注册：{list(available)}{hint}"
            )
        return provider, model

    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------

    def _prompt_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {
            "agent_id": self._agent_id,
            "session_id": self._session_id,
            "mode": self._options.mode,
        }
        provider = self._options.provider
        model = self._options.model
        if provider and model:
            context["provider"] = provider
            context["model"] = model
        workspace = self._ctx.get(WORKSPACE_SERVICE)
        if workspace is not None:
            context["cwd"] = str(workspace.root)
        return context

    def _render_prompt(self):
        prompt = self._ctx.require(PROMPT_SERVICE)
        return prompt.render(**self._prompt_context())

    @staticmethod
    def _trace(rendered) -> dict[str, Any]:
        return {
            "fingerprint": rendered.fingerprint,
            "sections": [
                {
                    "name": section.name,
                    "order": section.order,
                    "enabled": section.enabled,
                    "chars": len(section.text),
                }
                for section in rendered.sections
            ],
            "variables": dict(rendered.variables),
        }

    def _ensure_prompt_recorded(self, sessions) -> None:
        rendered = self._render_prompt()
        if rendered.fingerprint == self._last_prompt_fingerprint:
            return
        sessions.append(
            self._session_id,
            SYSTEM_MESSAGE,
            {"content": rendered.text, "prompt_trace": self._trace(rendered)},
        )
        self._last_prompt_fingerprint = rendered.fingerprint

    # ------------------------------------------------------------------
    # 句柄
    # ------------------------------------------------------------------

    def handle(self, *, parent: str | None) -> Any:
        backend = _Backend(
            session_id=self._session_id,
            run_turn=self.run_turn,
            steer=self.steer,
            inject=self.inject,
            cancel=self.cancel,
            dispose=self.dispose,
        )
        registry = self._ctx.require(AGENT_SERVICE)
        return registry.make_handle(
            self._agent_id, self._options, backend=backend, parent=parent
        )

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------

    def steer(self, text: str) -> None:
        with self._lock:
            self._pending_steps.append((text, "steer"))

    def inject(self, text: str) -> None:
        with self._lock:
            self._pending_steps.append((text, "inject"))

    def cancel(self) -> bool:
        self._cancel.set()
        return True

    def dispose(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_turn(self, text: str) -> TurnResult:
        ctx = self._ctx
        sessions = ctx.require(SESSIONS_SERVICE)
        llm = ctx.require(LLM_SERVICE)
        tools = ctx.require(TOOL_ADMIN_SERVICE)
        self._cancel.clear()

        self._ensure_prompt_recorded(sessions)
        turn = sessions.begin_turn(self._session_id, text)
        ctx.emit(AGENT_TURN_START, agent=self._agent_id, turn=turn, text=text)

        total = Usage()
        steps = 0
        final_text = ""
        status: str = "done"
        max_steps = self._options.max_steps
        if max_steps is None:
            max_steps = self._default_max_steps
        budget = self._options.token_budget
        if budget is None:
            budget = self._default_token_budget

        try:
            while True:
                if max_steps is not None and steps >= max_steps:
                    status = "max_steps"
                    break
                if self._cancel.is_set():
                    status = "cancelled"
                    break
                # 预算按**步边界**检查：本步的工具调用已经由模型请求、也已在
                # assistant 消息里落盘，必须先执行完再停——否则历史里会留下
                # 没有结果的 tool_calls，下一轮请求会被接口拒绝。
                if budget is not None and total.total_tokens > budget:
                    status = "budget_exceeded"
                    break
                steps += 1

                self._drain_pending_steps(sessions)
                messages = sessions.messages(self._session_id)
                sessions.append(
                    self._session_id, STEP_START, {"turn": turn, "step": steps}
                )
                ctx.emit(AGENT_STEP_START, agent=self._agent_id, turn=turn, step=steps)

                provider, model = self._route()
                specs = [
                    ToolSpec(
                        name=spec["name"],
                        description=spec.get("description", ""),
                        parameters=spec.get("parameters", {}),
                    )
                    for spec in tools.specs()
                ]
                request = llm.request(
                    messages,
                    provider=provider,
                    model=model,
                    max_tokens=self._options.max_tokens,
                    reasoning_effort=self._options.reasoning_effort,
                    tools=specs,
                    session_id=self._session_id,
                )
                # 策略插座：订阅者可以在分发前改写请求（例如注入工具限制）。
                request = ctx.waterfall(
                    AGENT_REQUEST,
                    request,
                    agent=self._agent_id,
                    turn=turn,
                    step=steps,
                )

                text_parts: list[str] = []
                accumulator = ToolCallAccumulator()
                step_usage: Usage | None = None
                for chunk in llm.stream(request):
                    if self._cancel.is_set():
                        status = "cancelled"
                        break
                    # 分片来自 llm 插件（它有自己的类型副本）——按 `type`
                    # 判别，不按 isinstance（插件之间不 import）。
                    kind = getattr(chunk, "type", None)
                    if kind == "text":
                        text_parts.append(chunk.text)
                        ctx.emit(
                            AGENT_DELTA,
                            agent=self._agent_id,
                            turn=turn,
                            step=steps,
                            text=chunk.text,
                        )
                    elif kind == "tool_calls":
                        accumulator.feed(chunk.deltas)
                    elif kind == "finish":
                        step_usage = chunk.usage
                if status == "cancelled":
                    # 已经流给界面的文本必须落盘（"已吐出的文本保留"）：
                    # 工具调用没有执行，所以不写 assistant 的 tool_calls ——
                    # 否则历史里会出现没有结果的悬空调用。
                    partial = "".join(text_parts)
                    if partial:
                        final_text = partial
                        sessions.append(
                            self._session_id,
                            ASSISTANT,
                            {
                                "content": partial,
                                "tool_calls": [],
                                "provider": provider,
                                "model": model,
                            },
                        )
                    sessions.append(
                        self._session_id,
                        STEP_END,
                        {
                            "turn": turn,
                            "step": steps,
                            "usage": _usage_dict(step_usage),
                        },
                    )
                    ctx.emit(
                        AGENT_STEP_END,
                        agent=self._agent_id,
                        turn=turn,
                        step=steps,
                        usage=step_usage,
                    )
                    break

                calls = accumulator.calls()
                content = "".join(text_parts)
                final_text = content
                sessions.append(
                    self._session_id,
                    ASSISTANT,
                    {
                        "content": content,
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in calls
                        ],
                        "provider": provider,
                        "model": model,
                    },
                )
                sessions.append(
                    self._session_id,
                    STEP_END,
                    {"turn": turn, "step": steps, "usage": _usage_dict(step_usage)},
                )
                ctx.emit(
                    AGENT_STEP_END,
                    agent=self._agent_id,
                    turn=turn,
                    step=steps,
                    usage=step_usage,
                )
                total = _add_usage(total, step_usage)

                if not calls:
                    # 没有后续调用：本轮已结束。但"超预算"仍然是事实，先如实
                    # 报告预算，再报完成（否则超出预算的最后一轮会被当成正常）。
                    if budget is not None and total.total_tokens > budget:
                        status = "budget_exceeded"
                    else:
                        status = "done"
                    break

                for call in calls:
                    sessions.append(
                        self._session_id,
                        TOOL_CALL,
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                    ctx.emit(
                        AGENT_TOOL_CALL,
                        agent=self._agent_id,
                        turn=turn,
                        step=steps,
                        call=call,
                    )
                    try:
                        arguments = call.parsed_arguments()
                        result = tools.execute(
                            call.name, arguments, session_id=self._session_id
                        )
                    except Exception as exc:  # noqa: BLE001 —— 参数坏了也是一次结果
                        result = _error_result(exc)
                    sessions.append(
                        self._session_id,
                        TOOL_RESULT,
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "status": result.status,
                            "content": result.content,
                            "message": result.message,
                            "error_code": result.error_code,
                        },
                    )
                    ctx.emit(
                        AGENT_TOOL_RESULT,
                        agent=self._agent_id,
                        turn=turn,
                        step=steps,
                        call=call,
                        result=result,
                    )
        except Exception as exc:  # noqa: BLE001 —— 失败以结构化状态收尾
            status = "error"
            ctx.emit(AGENT_REQUEST_ERROR, agent=self._agent_id, turn=turn, error=exc)
            sessions.append(
                self._session_id,
                TURN_END,
                {
                    "turn": turn,
                    "status": status,
                    "steps": steps,
                    "final_text": final_text,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            ctx.emit(
                AGENT_TURN_END,
                agent=self._agent_id,
                turn=turn,
                status=status,
                steps=steps,
                final_text=final_text,
                error=exc,
            )
            raise

        sessions.append(
            self._session_id,
            TURN_END,
            {
                "turn": turn,
                "status": status,
                "steps": steps,
                "final_text": final_text,
            },
        )
        ctx.emit(
            AGENT_TURN_END,
            agent=self._agent_id,
            turn=turn,
            status=status,
            steps=steps,
            final_text=final_text,
        )
        return TurnResult(
            turn=turn,
            status=status,
            final_text=final_text,
            steps=steps,
            usage=total if total.total_tokens else None,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _drain_pending_steps(self, sessions) -> None:
        with self._lock:
            pending, self._pending_steps = self._pending_steps, []
        for text, source in pending:
            sessions.append(
                self._session_id, USER_MESSAGE, {"text": text, "source": source}
            )


def _usage_dict(usage: Usage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
    }


def _add_usage(total: Usage, addition: Usage | None) -> Usage:
    if addition is None:
        return total
    return Usage(
        input_tokens=total.input_tokens + addition.input_tokens,
        output_tokens=total.output_tokens + addition.output_tokens,
        cached_input_tokens=total.cached_input_tokens + addition.cached_input_tokens,
    )


def _error_result(exc: BaseException):
    class ToolResult:
        def __init__(self, status, message="", error_code=None, content=None):
            self.status = status
            self.message = message
            self.error_code = error_code
            self.content = content

    return ToolResult(
        status="error",
        message=f"{type(exc).__name__}: {exc}",
        error_code="TOOL_EXCEPTION",
    )


def agent_loop_plugin(**config: Any) -> AgentLoopPlugin:
    """工厂：装配单的 `config` 可给默认 max_steps / token_budget。"""
    return AgentLoopPlugin(**config)


__all__ = ["AgentLoopPlugin", "LoopDriver", "agent_loop_plugin"]
