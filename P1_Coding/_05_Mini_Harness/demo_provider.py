"""离线规则 provider —— 让可视化页面在没有 API key 时也能"真"交互。

`FakeLLM` 是"剧本队列"：每条回复预先写好，看不到工具执行结果，因此离线演示里
最终回答只能是罐头文本。可视化页面希望看到"模型读到工具结果后再作答"这条完整轨迹，
所以这个 provider 改成**规则驱动 + 读历史**：

- 看最后一条消息：若是 `tool` 结果 → 把真实结果复述成中文回答（所以回答里带真数字）；
- 若是 `user` 消息 → 按关键词路由：文件列表 / 写文件 / 读文件 / 算术 → 发工具申请，
  都不是则直接闲聊回答。

它只用于 `--fake` 的离线可视化演示，**不参与任何验收测试**；真实对话请用
DeepSeekProvider。实现只依赖 `LLMProvider` 协议（见 provider.py）。
"""

from __future__ import annotations

import re
from typing import Any

from llm_seam.fake import text_reply
from llm_seam.provider import LLMRequest, LLMResponse
from llm_seam.vocabulary import Message, ToolCall, assistant

# 算术表达式：允许数字、括号、空白与四则运算符，且至少含一个运算符。
_ARITH = re.compile(r"[0-9][0-9\.\s]*[+\-*/%][0-9\.\s()+\-*/%]*")
# 路径样式的 token（用于写/读文件时猜一个文件名）。
_PATH = re.compile(r"[\w\u4e00-\u9fff\-/]+\.(?:txt|md|json|log|csv|py)")


def _last(role: str, messages: list[Message]) -> Message | None:
    for message in reversed(messages):
        if message.role == role:
            return message
    return None


def _pick_path(text: str, default: str) -> str:
    match = _PATH.search(text)
    return match.group(0) if match else default


def _route(text: str) -> ToolCall | None:
    """按关键词把用户输入路由到一次工具申请；都不匹配返回 None。"""
    lowered = text.lower()

    if any(word in text for word in ("哪些文件", "列出", "目录", "list")):
        return ToolCall(id="call_1", name="list_files", arguments={"path": "."})

    if any(word in text for word in ("写", "记到", "保存", "新建", "创建")):
        path = _pick_path(text, "notes/note.txt")
        content = text
        for marker in ("内容：", "内容:", "写：", "写:"):
            if marker in text:
                content = text.split(marker, 1)[1].strip()
                break
        # 去掉尾部"（写到|写入|保存到） <路径>"这类指令措辞，只留正文
        content = re.split(r"[，,。]?\s*(?:写到|写入|保存到|存到)\s*\S*", content)[0].strip()
        return ToolCall(
            id="call_1",
            name="write_file",
            arguments={"path": path, "content": content},
        )

    if any(word in text for word in ("读", "查看", "打开", "cat")):
        return ToolCall(id="call_1", name="read_file", arguments={"path": _pick_path(text, "notes/note.txt")})

    expression = _ARITH.search(text)
    if expression and any(op in expression.group(0) for op in "+-*/%"):
        return ToolCall(
            id="call_1",
            name="calculate",
            arguments={"expression": expression.group(0).strip()},
        )

    return None


def _find_call_name(messages: list[Message], call_id: str | None) -> str:
    for message in reversed(messages):
        for call in message.tool_calls:
            if call_id is None or call.id == call_id:
                return call.name
    return ""


def _summarize(messages: list[Message], tool_message: Message) -> str:
    """把最后一条工具结果复述成中文回答（读到什么说什么）。"""
    import json

    name = _find_call_name(messages, tool_message.tool_call_id)
    try:
        data: dict[str, Any] = json.loads(tool_message.content)
    except (ValueError, TypeError):
        return f"工具返回：{tool_message.content}"

    if "error" in data:
        return f"工具出错了：{data['error']}"
    if name == "calculate":
        return f"算出来是 {data.get('result')}。"
    if name == "write_file":
        return f"已写入 {data.get('written')}（{data.get('bytes')} 字节）。"
    if name == "read_file":
        return f"{data.get('path')} 的内容是：\n{data.get('content')}"
    if name == "list_files":
        files = data.get("files", [])
        listing = "、".join(files) if files else "（空）"
        return f"工作区里有：{listing}"
    return f"工具返回：{json.dumps(data, ensure_ascii=False)}"


class DemoProvider:
    """离线规则 provider（`--fake` 可视化演示用）。实现 LLMProvider 协议。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        messages = request.messages

        last = messages[-1] if messages else None
        if last is not None and last.role == "tool":
            return text_reply(_summarize(messages, last))

        user_message = _last("user", messages)
        text = user_message.content if user_message else ""
        call = _route(text)
        if call is not None:
            return LLMResponse(
                message=assistant("", [call]),
                finish_reason="tool_calls",
            )
        return text_reply(
            "（离线演示 provider）我听懂了你说的：“"
            + text
            + "”。加 --fake 只是为了可视化轨迹，完整对话请去掉 --fake 走真实 API。"
        )


__all__ = ["DemoProvider"]
