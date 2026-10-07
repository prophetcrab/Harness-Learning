"""turn / step 词汇 —— Agent 主循环的"调用轨迹"数据结构。

这是 _02 相对 _01 的核心升级：_01 的 run_tool_loop 只返回
(status, messages, steps, final_text) 四个扁平的字段；这里的轨迹把
"一次 turn 内部发生了什么"变成可事后审查的三层对象。

对应 dsh：agent-loop 把一次 turn（一次用户输入排空）拆成一串 step
（一次"模型请求 + 可能的工具执行"往返）。turn 与 step 的关系通过
Step.index（本 turn 内的第几步）落点：一个 turn 里可以有多个 step。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from harness.llm.provider import LLMRequest, LLMResponse
from harness.llm.vocabulary import Message

# 一个 turn 的三种收尾方式：
#   done       —— 模型给出最终回答
#   max_steps  —— 步数预算用尽仍未收尾（结构化终止，不是异常）
#   cancelled  —— 被外部取消（cancel() 或 should_stop 回调）
TurnStatus = Literal["done", "max_steps", "cancelled"]


@dataclass
class ToolResult:
    """一次工具执行的记录：谁申请的、拿到了什么。

    result 恒为字典；失败时是 {"error": ...} 而非抛异常 ——
    错误是给模型的输入（铁律 #7），同时也要留在轨迹里供审查。
    """

    call_id: str
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    error: bool


@dataclass
class Step:
    """一个 step：一次模型请求 +（若有工具申请）逐个工具执行。

    request 是发给模型的完整消息快照 —— 模型无状态，每步都要全量重发；
    轨迹里保留这份快照，正是"模型可见 ⟺ 已记录"（铁律 #1）的最朴素形态。
    """

    index: int
    request: LLMRequest
    response: LLMResponse
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass
class TurnResult:
    """一次 turn（一次用户输入）排空后的完整结果。

    messages 是 turn 结束时的完整历史投影（含之前 turn 累积的消息）。
    """

    turn: int
    status: TurnStatus
    steps: list[Step]
    messages: list[Message]
    final_text: str = ""  # status == "done" 时的回答文本
