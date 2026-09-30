"""最小工具闭环 —— 本练习的核心消费者：循环只依赖协议。

形状（和 P0 的手写循环相同，但这次不知道、也不需要知道背后是哪家模型）：

    while 还有步数预算:
        response = provider.complete(请求)        # ← 唯一的模型调用点
        history.append(response.message)          # 含 tool_calls 的回复必须保留
        if 没有工具申请: 结束，返回最终回答
        for 每次申请: 执行工具 → 结果作为 tool 消息回填

本模块是接下来 `_02_Agent_Loop` 的前身：_02 会把它升级成正式的 AgentLoop
类（turn/step 词汇、流式回调、运行轨迹、取消），但"只依赖 LLMProvider
协议"这条底线从本练习起就立住 —— 看 import 区：没有 openai。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Sequence

from llm_seam.provider import LLMProvider, LLMRequest
from llm_seam.vocabulary import Message, ToolSpec, tool_result

# 事件回调：kind 是事件名，payload 是本次事件的细节。
# 这是 dsh"事件是扩展点"最朴素的形态；_02 会把运行轨迹作为正式的一等对象。
EventHook = Callable[[str, dict[str, Any]], None]


@dataclass
class LoopResult:
    """一次闭环运行的完整结果。"""

    status: Literal["done", "max_steps"]  # 正常收尾 / 步数用尽
    messages: list[Message]               # 完整历史（可原样作为下次运行的输入）
    steps: int                            # 实际发生的模型调用次数
    final_text: str = ""                  # status == "done" 时的回答文本


def run_tool_loop(
    provider: LLMProvider,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    execute_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
    max_steps: int = 8,
    on_event: EventHook | None = None,
) -> LoopResult:
    """跑一轮"模型 ⇄ 工具"闭环。

    参数：
        provider     任意满足 LLMProvider 协议的对象（真实或假）
        messages     起始消息（通常是 system + user）
        tools        本会话可见的工具说明书
        execute_tool 执行器：名字 + 参数字典 → 结果字典
                     （约定：错误也要以 {"error": ...} 字典返回，不抛异常）
        max_steps    模型调用次数上限（防止模型停不下来）
        on_event     可选回调，用于观察每一轮（打印 / 记录 / 断言）
    """
    history: list[Message] = list(messages)
    tool_specs = list(tools)

    def emit(kind: str, **payload: Any) -> None:
        if on_event is not None:
            on_event(kind, payload)

    for step in range(1, max_steps + 1):
        # 快照历史再发请求：provider 拿到的是"本次调用的真实输入"，
        # 之后循环继续 append 也不会倒灌进已经发出的请求（测试会断言这一点）。
        request = LLMRequest(messages=list(history), tools=list(tool_specs))
        emit("request", step=step, message_count=len(history), tool_count=len(tool_specs))

        # ---- 唯一的模型调用点：协议在此，供应商在别处 ----
        response = provider.complete(request)

        # assistant 消息（即使只有 tool_calls、content 为空）也必须进历史
        history.append(response.message)
        emit(
            "response",
            step=step,
            finish_reason=response.finish_reason,
            tool_calls=[call.name for call in response.message.tool_calls],
            content=response.message.content,
        )

        # 没有工具申请 = 最终回答，闭环结束
        if not response.message.tool_calls:
            return LoopResult(
                status="done",
                messages=history,
                steps=step,
                final_text=response.message.content,
            )

        # 逐个执行工具；结果以 tool 消息回填（tool_call_id 与申请配对）
        for call in response.message.tool_calls:
            try:
                result = execute_tool(call.name, call.arguments)
            except Exception as exc:
                # 执行层兜底：工具炸了也不能炸掉整个闭环。
                # 错误是给模型的输入（铁律 #7），不是中断循环的理由。
                result = {"error": f"工具执行失败：{exc}"}
            history.append(tool_result(call.id, result))
            emit(
                "tool_result",
                step=step,
                name=call.name,
                arguments=call.arguments,
                result=result,
                is_error="error" in result,
            )

    # 预算用尽：返回现状而不是抛异常，由调用方决定怎么处理
    return LoopResult(status="max_steps", messages=history, steps=max_steps)
