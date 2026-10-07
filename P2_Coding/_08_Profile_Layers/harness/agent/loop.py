"""AgentLoop —— 正式的 agent 主循环（turn/step 词汇 + 轨迹 + 取消）。

_04 的副本，相对 _02/_03 有三处扩展，都是为了"可被会话日志记录 / 可从日志恢复"：

1. `initial_messages`：允许用外部历史（从日志投影出来的消息）初始化循环，实现 resume。
2. `initial_turn`：turn 计数可从外部接续，保证同一会话跨进程的 turn 编号连续。
3. 事件负载增强：`step_response` 携带完整 tool_calls（含 id/arguments）、`tool_result`
   携带 call_id、每步结束补发 `step_end` —— 让 on_event 流足以重建完整会话日志。

底线与 _01 相同：本模块只依赖 LLMProvider 协议，不 import 任何厂商 SDK。

对应 dsh：`packages/core/agent-loop/src/agent.ts` 的 turn/step 主流程。
差异说明：dsh 的 turn 由 inbox（异步输入队列）驱动，这里先用同步的
run(user_text) 顶替，inbox 留到后续主题（理由见 decisions/0002）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from harness.agent.trace import Step, ToolResult, TurnResult, TurnStatus
from harness.llm.provider import LLMProvider, LLMRequest
from harness.llm.vocabulary import Message, ToolSpec, system, tool_result, user

# 事件回调：循环通过它把"进行到哪了"流式抛给观察者（打印 / 记录 / 断言）。
EventHook = Callable[[str, dict[str, Any]], None]

# 工具执行器：名字 + 参数字典 → 结果字典（错误以 {"error": ...} 返回，不抛异常）。
ExecuteTool = Callable[[str, dict[str, Any]], dict[str, Any]]


class AgentLoop:
    """一次会话的主循环：维护历史、按 turn 排空用户输入。"""

    def __init__(
        self,
        provider: LLMProvider,
        execute_tool: ExecuteTool,
        max_steps: int = 8,
        system_prompt: str = "",
        on_event: EventHook | None = None,
        initial_messages: Sequence[Message] | None = None,
        initial_turn: int = 0,
    ) -> None:
        if max_steps < 1:
            raise ValueError(f"max_steps 必须 >= 1，收到 {max_steps}")
        if initial_messages is not None and system_prompt:
            raise ValueError("initial_messages 与 system_prompt 互斥：system 提示应已在历史里")
        self._provider = provider
        self._execute_tool = execute_tool
        self._max_steps = max_steps
        self._on_event = on_event

        # 会话历史：跨 turn 累积。resume 时由外部历史（日志投影）填充。
        if initial_messages is not None:
            self._history: list[Message] = deepcopy(list(initial_messages))
        else:
            self._history = [system(system_prompt)] if system_prompt else []

        self._turn = initial_turn
        self._cancelled = False

    # ------------------------------------------------------------------
    # 外部控制
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """请求取消（粘性）：下一次检查点（step 前）生效，之后所有 turn 都取消。"""
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def history(self) -> list[Message]:
        """当前会话历史的只读拷贝（这是投影，不直接暴露内部列表）。"""
        return deepcopy(self._history)

    @property
    def turn_count(self) -> int:
        """已经处理过的 turn 数。"""
        return self._turn

    # ------------------------------------------------------------------
    # 主入口：一次 turn
    # ------------------------------------------------------------------

    def run(
        self,
        user_text: str,
        tools: Sequence[ToolSpec],
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnResult:
        """处理一个 turn：把一条用户输入排空到最终回答 / 预算用尽 / 取消。"""
        self._turn += 1
        turn = self._turn
        self._history.append(user(user_text))
        self._emit("turn_start", turn=turn, user=user_text)

        steps: list[Step] = []

        for step_index in range(1, self._max_steps + 1):
            if self._stopped(should_stop):
                return self._finish(turn, "cancelled", steps)

            # 快照历史再发请求：之后继续 append 不会倒灌进已发出的请求。
            request = LLMRequest(messages=list(self._history), tools=list(tools))
            self._emit(
                "step_request",
                turn=turn,
                step=step_index,
                message_count=len(request.messages),
                tool_count=len(request.tools),
            )

            response = self._provider.complete(request)
            self._history.append(response.message)
            # tool_calls 带上完整信息（id/name/arguments）—— 会话日志靠它重建 assistant 消息。
            self._emit(
                "step_response",
                turn=turn,
                step=step_index,
                finish_reason=response.finish_reason,
                tool_calls=[
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in response.message.tool_calls
                ],
                content=response.message.content,
            )

            tool_results: list[ToolResult] = []
            for call in response.message.tool_calls:
                try:
                    result = self._execute_tool(call.name, call.arguments)
                except Exception as exc:
                    # 执行层兜底：工具炸了也不能炸掉整个循环（铁律 #7）。
                    result = {"error": f"工具执行失败：{exc}"}
                is_error = "error" in result
                self._history.append(tool_result(call.id, result))
                tool_results.append(
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                        result=result,
                        error=is_error,
                    )
                )
                self._emit(
                    "tool_result",
                    turn=turn,
                    step=step_index,
                    call_id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                    result=result,
                    is_error=is_error,
                )

            steps.append(
                Step(
                    index=step_index,
                    request=request,
                    response=response,
                    tool_results=tool_results,
                )
            )
            self._emit("step_end", turn=turn, step=step_index)

            if not response.message.tool_calls:
                return self._finish(turn, "done", steps, response.message.content)

        # 预算用尽：结构化终止，由调用方决定怎么处理。
        return self._finish(turn, "max_steps", steps)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _stopped(self, should_stop: Callable[[], bool] | None) -> bool:
        return self._cancelled or bool(should_stop is not None and should_stop())

    def _finish(
        self,
        turn: int,
        status: TurnStatus,
        steps: list[Step],
        final_text: str = "",
    ) -> TurnResult:
        result = TurnResult(
            turn=turn,
            status=status,
            steps=steps,
            messages=list(self._history),
            final_text=final_text,
        )
        self._emit(
            "turn_end",
            turn=turn,
            status=status,
            steps=len(steps),
            final_text=final_text,
        )
        return result

    def _emit(self, kind: str, **payload: Any) -> None:
        if self._on_event is not None:
            self._on_event(kind, payload)
