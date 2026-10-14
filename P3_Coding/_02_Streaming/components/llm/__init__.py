"""LLM 构件 —— 中立词汇 + provider 协议 + 两种实现（Fake / DeepSeek）。

P1 `_01` 的接缝，原样沿用：厂商方言关在 deepseek.py 里，循环只见协议。
"""

from components.llm.deepseek import DeepSeekProvider
from components.llm.fake import FakeLLM, text_reply, tool_call_reply, tool_calls_reply
from components.llm.provider import LLMProvider, LLMRequest, LLMResponse
from components.llm.vocabulary import (
    Message,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
    assistant,
    system,
    tool_result,
    user,
)

__all__ = [
    "DeepSeekProvider",
    "FakeLLM",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "Role",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "assistant",
    "system",
    "text_reply",
    "tool_call_reply",
    "tool_calls_reply",
    "tool_result",
    "user",
]
