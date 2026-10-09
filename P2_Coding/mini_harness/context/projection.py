"""投影扩展 —— 让 system/message 事件参与"模型历史"的派生，并支持装配单追溯。

基线的投影（`harness.session.projection`）只认 `session/start` 里的系统提示词，
并把它放在消息列表开头。`_02` 新增的 `system/message` 是**每 step 渲染的更新**；
派生语义是"**后一条遮蔽前一条**"——最近一次的渲染文本生效，日志本身一条不动
（铁律 #2：修改不重写历史，而是用新事实遮蔽旧事实）。

于是"当前模型看到的 system 消息"有了唯一答案：

    effective_system_prompt(events) = 最近的 system/message，否则 session/start 的初始值

`_03` 在同一批事件上再加一条读取："当前生效的**装配单**"（latest_prompt_trace）——
它是文本的来源记录；`prompt.rebuild_text(装配单) == 生效文本` 即"可由日志重建"。

投影结果里仍然只有**一条** system 消息——与包装 provider 实际发送给模型的
请求形状一致（循环的消息列表里 system 只有一个位置）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from harness.llm.vocabulary import Message, system
from harness.session import derive_messages as derive_baseline_messages
from harness.session.events import Event


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
    """当前生效的装配单（原始 dict，可直接交给 prompt.rebuild_text 重建）。

    与 effective_system_prompt 走同一条遮蔽链：最近的 system/message 装配单，
    否则 session/start 的初始装配单。若两者都没有（如 `_02` 时代写的老日志），
    返回 None —— 追溯不到"怎么拼出来的"，但文本本身仍可取到。
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


def project(events: Iterable[Event]) -> list[Message]:
    """基线投影 + system/message 遮蔽：结果里仍只有一条 system 消息（最新的那条）。"""
    events = list(events)
    messages = derive_baseline_messages(events)
    text = effective_system_prompt(events)
    if text is None:
        return messages
    if messages and messages[0].role == "system":
        messages[0] = system(text)
    else:
        messages.insert(0, system(text))
    return messages


__all__ = ["effective_system_prompt", "latest_prompt_trace", "project"]

