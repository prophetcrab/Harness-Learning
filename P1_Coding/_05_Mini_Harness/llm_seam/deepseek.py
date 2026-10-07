"""DeepSeekProvider —— 真实供应商实现，负责"方言翻译"。

这个文件是"翻译边界"的教科书例子。它吸收掉的全部方言细节：
- arguments 在线上是 JSON 字符串，在词汇里是字典（双向翻译）；
- tool_calls 的嵌套结构 {id, type, function:{name, arguments}}；
- 工具结果的配对字段叫 tool_call_id；
- finish_reason 的取值（"stop" / "tool_calls"）。

翻译函数刻意做成模块级纯函数（可以离线单独测试，见
test_provider_protocol.py 里的翻译测试）；类本身只做"接线"。

对应 dsh：`packages/llm/llm-deepseek/src/adapter.ts` + `sse.ts`。
"""

from __future__ import annotations

import json
from typing import Any

from llm_seam.provider import LLMRequest, LLMResponse
from llm_seam.vocabulary import Message, ToolCall, ToolSpec, Usage

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


# ---------------------------------------------------------------------------
# 中立词汇 → 线上方言
# ---------------------------------------------------------------------------


def message_to_wire(message: Message) -> dict[str, Any]:
    """把一条中立消息翻译成 OpenAI 兼容的线上格式。"""
    if message.role in ("system", "user"):
        return {"role": message.role, "content": message.content}

    if message.role == "assistant":
        wire: dict[str, Any] = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        # 关键翻译：字典 → JSON 字符串（线上格式的硬性要求）
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return wire

    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,  # 必须是某次申请的 id
            "content": message.content,
        }

    raise ValueError(f"未知角色：{message.role}")


def tool_to_wire(spec: ToolSpec) -> dict[str, Any]:
    """把一条工具说明书翻译成线上 tools 字段的格式。"""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


# ---------------------------------------------------------------------------
# 线上方言 → 中立词汇
# ---------------------------------------------------------------------------


def response_from_api(api_response: Any) -> LLMResponse:
    """把一次 SDK 返回翻译成中立响应。

    参数类型标 Any 是刻意的：本函数不依赖 openai 的类定义，
    测试用 SimpleNamespace 假对象就能驱动它（见测试文件）。
    """
    choice = api_response.choices[0]
    raw = choice.message

    tool_calls = [
        ToolCall(
            id=call.id,
            name=call.function.name,
            # 关键翻译：JSON 字符串 → 字典
            arguments=json.loads(call.function.arguments or "{}"),
        )
        for call in (raw.tool_calls or [])
    ]

    usage = None
    if getattr(api_response, "usage", None) is not None:
        usage = Usage(
            prompt_tokens=api_response.usage.prompt_tokens,
            completion_tokens=api_response.usage.completion_tokens,
        )

    return LLMResponse(
        message=Message(
            role="assistant",
            content=raw.content or "",  # 纯工具申请时 content 可能是 None
            tool_calls=tool_calls,
        ),
        finish_reason=choice.finish_reason or "stop",
        usage=usage,
    )


# ---------------------------------------------------------------------------
# Provider 本体
# ---------------------------------------------------------------------------


class DeepSeekProvider:
    """通过 OpenAI 兼容协议调用 DeepSeek。"""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
    ) -> None:
        # 延迟导入：让本模块的翻译函数可以脱离 openai 单独使用（测试就靠这个）。
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model

    def complete(self, request: LLMRequest) -> LLMResponse:
        """协议实现：翻译 → 调用 → 翻译回来。"""
        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [message_to_wire(m) for m in request.messages],
        }
        if request.tools:
            kwargs["tools"] = [tool_to_wire(spec) for spec in request.tools]

        api_response = self._client.chat.completions.create(**kwargs)
        return response_from_api(api_response)
