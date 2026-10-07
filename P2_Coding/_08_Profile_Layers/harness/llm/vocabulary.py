"""词汇表 —— harness 内部描述"模型对话"的中立数据结构。

本文件是整个接缝的"公共语言"：
- 循环（loop.py）、工具（tools.py）、测试都只认这里的类型；
- 任何厂商专属字段（choices、function_call、JSON 字符串形式的 arguments）
  都不允许出现在这里。

对应 dsh：`packages/llm/llm/src/message.ts` —— 先定义中立的 message/stream
词汇，再由各 adapter（如 llm-deepseek）翻译成厂商格式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

# 四种角色。tool 消息装工具执行结果；其余三种与 chat API 的惯例一致。
Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    """模型发出的一次"工具调用申请"。

    注意 arguments 是已经解析好的字典：
    厂商 API 实际返回的是 JSON 字符串（我们实测过 `'{"expression": "1+1"}'`），
    解析工作由 provider 完成 —— 循环和工具层永远不用碰字符串。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """工具的"说明书"（给模型看的，不是实现）。

    parameters 用 JSON Schema 的 object 片段描述参数；
    这份结构会被 provider 翻译成各家的 tools 字段格式。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    """一次调用的 token 用量（各家 API 都提供类似字段）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Message:
    """一条对话消息。四个 role 共用一个结构，按 role 使用不同字段：

    - system / user  ：只用 content
    - assistant      ：content 和/或 tool_calls（可以同时有 —— 模型一边说
                       "我来查一下"一边发申请，这是实测过的真实行为）
    - tool           ：content 装工具结果，tool_call_id 必须与某次申请配对

    协议细节（P0 踩过的坑）：assistant 的 tool_calls 消息必须原样进入下一轮
    历史，tool 结果必须携带对应的 tool_call_id —— 模型靠它们对应
    "哪次申请拿到了什么结果"。漏掉任何一边，模型下一轮就会失忆。
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


# ---------------------------------------------------------------------------
# 构造函数：拼消息时比手写 Message(...) 更不容易错
# ---------------------------------------------------------------------------


def system(text: str) -> Message:
    """系统提示词消息。"""
    return Message(role="system", content=text)


def user(text: str) -> Message:
    """用户消息。"""
    return Message(role="user", content=text)


def assistant(text: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
    """助手消息：可以只有文本、只有工具申请、或两者兼有。"""
    return Message(role="assistant", content=text, tool_calls=list(tool_calls or []))


def tool_result(call_id: str, result: Any) -> Message:
    """工具结果消息。

    dict/list 会自动序列化成 JSON 文本（模型读 JSON 很自然）；
    字符串原样保留。call_id 必须来自对应那次 ToolCall.id。
    """
    if isinstance(result, str):
        content = result
    else:
        content = json.dumps(result, ensure_ascii=False)
    return Message(role="tool", content=content, tool_call_id=call_id)
