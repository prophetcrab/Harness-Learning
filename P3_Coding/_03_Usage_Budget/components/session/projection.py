"""投影 —— 把事件日志投影成"模型历史"（derive_messages）。

纪律（沿用 P1 `_04`）：消息永远由日志派生，绝不直接存储；
日志是唯一真相，模型历史只是它的一个视图。

本包在基线投影之上合并了两条 P2 扩展（`_02`/`_03` 的成果），现在是一体的：

- `system/message` 事件参与投影，语义是"**后一条遮蔽前一条**"——每 step
  渲染的最新系统提示词生效，日志本身一条不动（修改不重写历史，而是用新
  事实遮蔽旧事实）；
- `effective_system_prompt` / `latest_prompt_trace` 回答"当前生效的文本 /
  装配单是什么"：最近一条 `system/message`，否则 `session/start` 的初始值。

映射关系（只有这五类事件参与消息投影）：
    session/start     → system 消息（若有 system_prompt）
    system/message    → 遮蔽 system 文本（最近的生效）
    user/message      → user 消息
    assistant/message → assistant 消息（含 tool_calls）
    tool/result       → tool 消息（tool_call_id 与申请配对）

生命周期事件（turn/step 的 start/end）不产生消息，只用于观测与恢复边界。
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from components.llm.vocabulary import Message, ToolCall, assistant, system, tool_result, user
from components.session.events import Event


def effective_system_prompt(events: Iterable[Event]) -> str | None:
    """当前生效的系统提示词：最近一次 system/message；否则 session/start 的初始文本。

    两者都没有（或为空串）时返回 None —— 表示"这个会话没有系统提示词"。
    """
    latest: str | None = None
    started: str | None = None
    for event in events:
        if event.type == "system/message":
            latest = event.data.get("content", "")
        elif event.type == "session/start" and started is None:
            started = event.data.get("system_prompt", "")
    text = latest if latest is not None else started
    return text or None


def latest_prompt_trace(events: Iterable[Event]) -> dict[str, Any] | None:
    """当前生效的装配单（原始 dict，可直接交给 rebuild_text 重建）。

    与 effective_system_prompt 走同一条遮蔽链：最近的 system/message 装配单，
    否则 session/start 的初始装配单；都没有时返回 None（追溯不到"怎么拼出来的"，
    但文本本身仍可取到）。
    """
    latest: dict[str, Any] | None = None
    started: dict[str, Any] | None = None
    for event in events:
        if event.type == "system/message":
            trace = event.data.get("prompt_trace")
            if trace is not None:
                latest = trace
        elif event.type == "session/start" and started is None:
            started = event.data.get("prompt_trace")
    return latest if latest is not None else started


def _base_messages(events: list[Event]) -> list[Message]:
    """不含 system/message 遮蔽的基线投影（session/start 的 system 原样出现）。"""
    messages: list[Message] = []
    for event in events:
        if event.type == "session/start":
            prompt = event.data.get("system_prompt", "")
            if prompt:
                messages.append(system(prompt))
        elif event.type == "user/message":
            messages.append(user(event.data["content"]))
        elif event.type == "assistant/message":
            calls = [
                ToolCall(
                    id=call["id"],
                    name=call["name"],
                    arguments=deepcopy(call.get("arguments", {})),
                )
                for call in event.data.get("tool_calls", [])
            ]
            messages.append(assistant(event.data.get("content", ""), calls))
        elif event.type == "tool/result":
            messages.append(tool_result(event.data["call_id"], event.data["result"]))
    return messages


def derive_messages(events: Iterable[Event]) -> list[Message]:
    """从事件序列投影出模型历史；system 消息取"当前生效"的那一条。"""
    materialized = list(events)
    messages = _base_messages(materialized)
    text = effective_system_prompt(materialized)
    if text is None:
        return messages
    if messages and messages[0].role == "system":
        messages[0] = system(text)
    else:
        messages.insert(0, system(text))
    return messages


__all__ = [
    "derive_messages",
    "effective_system_prompt",
    "latest_prompt_trace",
]
