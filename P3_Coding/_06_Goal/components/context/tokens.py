"""上下文计量构件 —— 字符→token 估算、请求构成拆分（`_04` 起，`_05` 起共用）。

**为什么不用 tokenizer**：本练习不引第三方依赖，也不需要精确到个位。规则是
"**占比来自估算、总量来自实测**"——估算给构成比例，实测（响应的
`usage.prompt_tokens`）纠正总量，两者各取所长。见 `plugins/context_meter.py`。

比例如下（DeepSeek 官方给的经验值）：

    中文（含全角标点）  ≈ 0.6 token/字
    其他（ASCII 等）    ≈ 0.3 token/字符

两个消费者：

    plugins/context_meter.py      仪表：四类占比 → 按实测总额缩放
    components/agent/compact.py   压缩：触发判断、保留区预算、摘要长度上限

`DEFAULT_CONTEXT_WINDOW` 是本包对"模型窗口有多大"的唯一默认值（`_05` 起
改成 1M——DeepSeek 长上下文档），入口、仪表、压缩都从这里取，避免三处各写一个数。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from components.llm.vocabulary import Message, ToolSpec

#: 默认上下文窗口（DeepSeek 长上下文档 1M）。可配：`--context-window`。
DEFAULT_CONTEXT_WINDOW = 1_048_576

#: 请求构成的四类（固定顺序：对齐"从大到小的直觉"，页面按此顺序显示）。
PART_ORDER = ("消息", "系统工具", "系统提示词", "其他")

#: 每条消息的固定开销（角色标记、分隔符等聊天模板成本）。
MESSAGE_OVERHEAD = 4.0

#: 请求整体的固定开销（模板头部/尾部）。
REQUEST_OVERHEAD = 8.0


def _is_cjk(ch: str) -> bool:
    """是不是中日韩字符（含全角标点）——估算比例不同。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # 统一汉字
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角字符
    )


def estimate_tokens(text: str) -> float:
    """按经验比例估算 token 数：中文 ≈ 0.6/字，其他 ≈ 0.3/字符。"""
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk * 0.6 + (len(text) - cjk) * 0.3


def serialize_message(message: Message) -> str:
    """把一条消息摊平成文本（估算用——工具参数与结果也要算进去）。"""
    pieces = [message.role, message.content or ""]
    for call in message.tool_calls:
        pieces.append(call.name)
        pieces.append(json.dumps(call.arguments, ensure_ascii=False))
    if message.tool_call_id:
        pieces.append(message.tool_call_id)
    return "\n".join(pieces)


def message_tokens(message: Message) -> float:
    """一条消息的估算 token（正文 + 工具参数 + 每条的消息开销）。"""
    return estimate_tokens(serialize_message(message)) + MESSAGE_OVERHEAD


def measure_parts(messages: Iterable[Message], tools: Iterable[ToolSpec]) -> dict[str, float]:
    """把一次请求拆成四类并估算 token（纯函数，可单测）。

    - 系统提示词：system 消息的正文（每 step 重渲染后的那一份）；
    - 系统工具：工具说明书（名字/描述/JSON Schema）；
    - 消息：user / assistant / tool 消息（含工具调用的参数与结果）；
    - 其他：每条消息的角色标记、分隔符等聊天模板开销（按 4/条 + 8 估）。
    """
    system_text: list[str] = []
    conversation: list[str] = []
    for message in messages:
        if message.role == "system":
            system_text.append(message.content or "")
        else:
            conversation.append(serialize_message(message))

    tools_text = json.dumps(
        [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in tools
        ],
        ensure_ascii=False,
    )
    message_count = len(system_text) + len(conversation)
    return {
        "消息": estimate_tokens("\n".join(conversation)),
        "系统工具": estimate_tokens(tools_text),
        "系统提示词": estimate_tokens("\n".join(system_text)),
        "其他": MESSAGE_OVERHEAD * message_count + REQUEST_OVERHEAD,
    }


def request_tokens(messages: Iterable[Message], tools: Iterable[ToolSpec]) -> float:
    """整个请求的估算 token（四类之和）——压缩的触发判断用这个。"""
    return sum(measure_parts(messages, tools).values())


def as_int(value: float) -> int:
    """估算值转整数（只用于展示与日志，别拿它当精确值）。"""
    return int(round(value))


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "MESSAGE_OVERHEAD",
    "PART_ORDER",
    "REQUEST_OVERHEAD",
    "as_int",
    "estimate_tokens",
    "measure_parts",
    "message_tokens",
    "request_tokens",
    "serialize_message",
]
