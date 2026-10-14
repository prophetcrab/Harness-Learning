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

import itertools
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from components.agent.trace import Step, ToolResult, TurnResult, TurnStatus
from components.llm.provider import LLMProvider, LLMRequest, LLMResponse
from components.llm.streaming import StreamChunk, aggregate_chunks
from components.llm.vocabulary import Message, ToolSpec, Usage, system, tool_result, user

# 事件回调：循环通过它把"进行到哪了"流式抛给观察者（打印 / 记录 / 断言）。
EventHook = Callable[[str, dict[str, Any]], None]

# 工具执行器：名字 + 参数字典 → 结果字典（错误以 {"error": ...} 返回，不抛异常）。
ExecuteTool = Callable[[str, dict[str, Any]], dict[str, Any]]


class AgentLoop:
    """一次会话的主循环：维护历史、按 turn 排空用户输入。

    `max_steps=None`（默认）= **不限步数**：长程任务（多轮工具往返）不会被
    中途截断。要设上限也可以显式传数字——那是给"必须限住"的场景（如测试里
    验证触顶行为）用的，触顶时以结构化状态 `max_steps` 收尾而非抛异常。
    """

    def __init__(
        self,
        provider: LLMProvider,
        execute_tool: ExecuteTool,
        max_steps: int | None = None,
        system_prompt: str = "",
        on_event: EventHook | None = None,
        initial_messages: Sequence[Message] | None = None,
        initial_turn: int = 0,
        token_budget: int | None = None,
    ) -> None:
        if max_steps is not None and max_steps < 1:
            raise ValueError(f"max_steps 必须是 None（不限）或 >= 1，收到 {max_steps}")
        if token_budget is not None and token_budget < 1:
            raise ValueError(f"token_budget 必须是 None（不限）或 >= 1，收到 {token_budget}")
        if initial_messages is not None and system_prompt:
            raise ValueError("initial_messages 与 system_prompt 互斥：system 提示应已在历史里")
        self._provider = provider
        self._execute_tool = execute_tool
        self._max_steps = max_steps
        #: 单个 turn 的 token 预算上限（None = 不限）。跨 step 累计；
        #: 超限时以结构化状态 `budget_exceeded` 收尾，而不是抛异常。
        self._token_budget = token_budget
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
        turn_usage = Usage()

        # 不限步数（max_steps=None）时用无限计数：循环只在"得出最终回答"或
        # "被取消"时退出；设了上限时才可能出现下面的 max_steps 收尾。
        step_indices = (
            itertools.count(1) if self._max_steps is None
            else range(1, self._max_steps + 1)
        )
        for step_index in step_indices:
            if self._stopped(should_stop):
                return self._finish(turn, "cancelled", steps, usage=turn_usage)

            # 快照历史再发请求：之后继续 append 不会倒灌进已发出的请求。
            request = LLMRequest(messages=list(self._history), tools=list(tools))
            self._emit(
                "step_request",
                turn=turn,
                step=step_index,
                message_count=len(request.messages),
                tool_count=len(request.tools),
            )

            # 流式（可选能力）：provider 有 stream() 就边到边发增量，
            # 最后聚合成与 complete() 完全相同的 response——循环的其余部分
            # 感知不到差别（"流式只是多了一条观察通道，不是另一套语义"）。
            response, stream_cancelled = self._complete_or_stream(
                request, turn, step_index, should_stop
            )
            self._history.append(response.message)
            if response.usage is not None:
                turn_usage = turn_usage + response.usage
                self._emit(
                    "step_usage",
                    turn=turn,
                    step=step_index,
                    usage=response.usage,
                    turn_total=turn_usage.total_tokens,
                )
            if stream_cancelled:
                step = Step(index=step_index, request=request, response=response, tool_results=[])
                steps.append(step)
                self._emit("step_end", turn=turn, step=step_index)
                return self._finish(turn, "cancelled", steps, response.message.content, turn_usage)
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

            # 预算检查：超限就收尾（结构化 status，不是异常）。
            # 位置刻意放在"判定完成"之前——超预算的 turn 不该再假装正常收尾，
            # 否则调用方看到 done 却不知道这一次已经花超了。
            if self._token_budget is not None and turn_usage.total_tokens > self._token_budget:
                self._emit(
                    "budget_exceeded",
                    turn=turn,
                    used=turn_usage.total_tokens,
                    budget=self._token_budget,
                    partial_text=response.message.content,
                )
                return self._finish(
                    turn,
                    "budget_exceeded",
                    steps,
                    response.message.content,
                    turn_usage,
                )

            if not response.message.tool_calls:
                return self._finish(turn, "done", steps, response.message.content, turn_usage)

        # 仅在显式设了 max_steps 时可达：结构化终止，由调用方决定怎么处理。
        return self._finish(turn, "max_steps", steps, usage=turn_usage)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _complete_or_stream(
        self,
        request: LLMRequest,
        turn: int,
        step_index: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[LLMResponse, bool]:
        """一次模型调用：支持流式就逐段发 `step_delta`，否则退回 complete()。

        返回 (响应, 是否被取消)。取消不假装正常：中途被打断的 turn 必须以
        `cancelled` 收尾（保留已收到的文本），而不是拿半截回复当"最终回答"。
        """
        stream = getattr(self._provider, "stream", None)
        if stream is None:
            return self._provider.complete(request), False

        generator = stream(request)
        chunks: list[StreamChunk] = []
        cancelled = False
        try:
            for chunk in generator:
                if self._stopped(should_stop):
                    # 取消（cancel() 或 should_stop 回调）：关掉上游
                    # （provider 的 finally 会收尾），保留已收到的片段
                    generator.close()
                    cancelled = True
                    self._emit(
                        "stream_cancelled",
                        turn=turn,
                        step=step_index,
                        received_chars=sum(len(c.text) for c in chunks),
                    )
                    break
                chunks.append(chunk)
                self._emit(
                    "step_delta",
                    turn=turn,
                    step=step_index,
                    text=chunk.text,
                    tool_index=chunk.tool_index,
                )
        except Exception:
            generator.close()
            raise
        response = aggregate_chunks(chunks)
        if cancelled:
            response.message.content = response.message.content  # 保留部分文本
            response.message.tool_calls = []                     # 半截的工具调用不予执行
            response.finish_reason = "cancelled"
        return response, cancelled

    def _stopped(self, should_stop: Callable[[], bool] | None) -> bool:
        return self._cancelled or bool(should_stop is not None and should_stop())

    def _finish(
        self,
        turn: int,
        status: TurnStatus,
        steps: list[Step],
        final_text: str = "",
        usage: Usage | None = None,
    ) -> TurnResult:
        result = TurnResult(
            turn=turn,
            status=status,
            steps=steps,
            messages=list(self._history),
            final_text=final_text,
            usage=usage if usage is not None else Usage(),
        )
        self._emit(
            "turn_end",
            turn=turn,
            status=status,
            steps=len(steps),
            final_text=final_text,
            usage=result.usage,
        )
        return result

    def _emit(self, kind: str, **payload: Any) -> None:
        if self._on_event is not None:
            self._on_event(kind, payload)
