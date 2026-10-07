"""AgentLoop —— 正式的 agent 主循环（turn/step 词汇 + 轨迹 + 取消）。

它把 _01 的 run_tool_loop 函数升级成可复用的类，补上三样 _01 没有的东西：

1. turn/step 词汇 —— 一次 run() 是一个 turn；turn 内每轮模型往返是一个 step。
   历史由循环自己持有，跨 turn 累积（这就是"turn 有边界"的意义）。
2. 调用轨迹 —— run() 返回 TurnResult（每个 Step 的请求/回复/工具结果都能
   事后审查），不再只靠 on_event 打印。
3. 可取消 —— turn 之间、step 之间都能被外部打断（cancel() 或 should_stop
   回调），结构化返回 status="cancelled"。

底线与 _01 相同：本模块只依赖 LLMProvider 协议，不 import 任何厂商 SDK。

对应 dsh：`packages/core/agent-loop/src/agent.ts` 的 turn/step 主流程。
差异说明：dsh 的 turn 由 inbox（异步输入队列）驱动，这里先用同步的
run(user_text) 顶替，inbox 留到后续主题（理由见 decisions/0002）。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from llm_seam.provider import LLMProvider, LLMRequest
from llm_seam.vocabulary import Message, ToolSpec, system, tool_result, user

from agent_loop.trace import Step, ToolResult, TurnResult, TurnStatus

# 事件回调：循环通过它把"进行到哪了"流式抛给观察者（打印 / 记录 / 断言）。
# 与 _01 相同的朴素形态（kind + payload 字典），只是事件集合扩展到了 turn 边界。
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
    ) -> None:
        if max_steps < 1:
            raise ValueError(f"max_steps 必须 >= 1，收到 {max_steps}")
        self._provider = provider
        self._execute_tool = execute_tool
        self._max_steps = max_steps
        self._on_event = on_event

        # 会话历史：跨 turn 累积。system 提示只在构造时注入一次。
        self._history: list[Message] = []
        if system_prompt:
            self._history.append(system(system_prompt))

        self._turn = 0
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
        """当前会话历史的只读拷贝（勿改返回值；这是投影，不直接暴露内部列表）。"""
        return list(self._history)

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
        """处理一个 turn：把一条用户输入排空到最终回答 / 预算用尽 / 取消。

        should_stop：可选的取消谓词，在每一步（模型调用）之前检查；
        返回 True 即中断。cancel() 与它叠加，任一成立都停。
        """
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
            self._emit(
                "step_response",
                turn=turn,
                step=step_index,
                finish_reason=response.finish_reason,
                tool_calls=[call.name for call in response.message.tool_calls],
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
