"""投影 —— 把事件日志投影成"模型历史"（derive_messages）。

纪律（沿用 P1 `_04`）：消息永远由日志派生，绝不直接存储；
日志是唯一真相，模型历史只是它的一个视图。

本包在基线投影之上合并了两条 P2 扩展与一条 P3 `_05` 扩展，现在是一体的：

- `system/message` 事件参与投影，语义是"**后一条遮蔽前一条**"——每 step
  渲染的最新系统提示词生效，日志本身一条不动（修改不重写历史，而是用新
  事实遮蔽旧事实）；
- `compact/checkpoint` 事件参与投影（`_05`），语义是"**折叠**"——该事件声明
  "写它的时候，对话列表最前面的 `folded` 条已折叠成这句话"；投影按**位置**丢掉
  这 `folded` 条、把摘要顶到最前。日志一条不动，所以**重放与在线时同构**
  （重新装载日志得到的历史与当时发给模型的一致）；
- `effective_system_prompt` / `latest_prompt_trace` 回答"当前生效的文本 /
  装配单是什么"：最近一条 `system/message`，否则 `session/start` 的初始值。

**日志被截断时的补齐（只删不造）**：日志可能被外部破坏——最典型的是**头部被截**
（文件被删掉一段、或人工清理过），于是投影出的第一条消息可能是某个工具结果，
它对应的 `assistant(tool_calls)` 已经不在了；进程在"模型刚要调工具"与"工具结果
落盘"之间被杀，也会留下"调用了但没有结果"。这两种形状**都会被 API 拒绝**：

    Messages with role 'tool' must be a response to a preceding message with 'tool_calls'

于是那条会话**再也发不出任何请求**（每次 400），而用户只看到"出错"。`sanitize_pairs`
按"只删不造"的原则修：孤儿工具结果丢掉、没人应答的调用从 assistant 消息里摘掉
（摘空了就整条丢）。**绝不凭空编一条结果**——那会违反"模型可见即已记录"。
日志本身一个字节都不动，重放仍同构（每次重放都做同样的修剪）。

映射关系（只有这几类事件参与消息投影）：
    session/start     → system 消息（若有 system_prompt）
    system/message    → 遮蔽 system 文本（最近的生效）
    user/message      → user 消息
    assistant/message → assistant 消息（含 tool_calls）
    tool/result       → tool 消息（tool_call_id 与申请配对）
    compact/checkpoint→ 丢掉对话列表最前的 folded 条，换成一条摘要消息

折叠为什么按**位置**而不是按 seq：摘要自己带 seq（=checkpoint 的 seq），而它被
放在列表最前——列表里的 seq 因此不再单调（第 1 条可能比第 2 条的 seq 大）。
按"seq ≤ X"丢会连带丢掉本该保留的更早消息（实测踩到：折 1 条却掉了 3 条，
内存历史与日志投影就此失同步）。按位置丢与循环内存里的切片
（`history[start : start+folded]`）逐条对应，两个视图天然对齐。

折叠的边界纪律：**system 永不参与折叠**（它的生效链由 system/message 遮蔽决定，
折它会把遮蔽链搅乱）；摘要成为列表第一条——于是下一次压缩能把这个摘要连同
它之后的尾巴一起再折进去（摘要不堆积、不链式膨胀）。

生命周期事件（turn/step 的 start/end）不产生消息，只用于观测与恢复边界。
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from components.llm.vocabulary import (
    Message,
    ToolCall,
    assistant,
    system,
    tool_result,
    user,
)
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


def _message_from_event(event: Event) -> Message | None:
    """一条事件 → 一条对话消息（不产生消息的事件返回 None）。"""
    if event.type == "user/message":
        return user(event.data["content"])
    if event.type == "assistant/message":
        calls = [
            ToolCall(
                id=call["id"],
                name=call["name"],
                arguments=deepcopy(call.get("arguments", {})),
            )
            for call in event.data.get("tool_calls", [])
        ]
        return assistant(event.data.get("content", ""), calls)
    if event.type == "tool/result":
        return tool_result(event.data["call_id"], event.data["result"])
    return None


def conversation_pairs(events: Iterable[Event]) -> list[tuple[int, Message]]:
    """投影出"对话消息 + 来源 seq"的列表（system 不在内；checkpoint 已生效）。

    返回 [(seq, message), ...]：seq 是产出这条消息的事件序号；摘要消息的 seq
    是它那条 checkpoint 的 seq。**这个列表按事件发生顺序排列，但 seq 不保证单调**
    （摘要的 seq 比它后面那些消息大）——所以折叠按位置做，别按 seq 比较。

    折叠越界（folded 超过当时的消息数）直接报错：那意味着日志被外部改过，
    或者循环与日志真的失同步了；静默夹紧会让"重放同构"变成一句空话。
    """
    pairs: list[tuple[int, Message]] = []
    for event in events:
        if event.type == "compact/checkpoint":
            folded = int(event.data["folded"])
            if not 0 <= folded <= len(pairs):
                raise ValueError(
                    f"compact/checkpoint(seq={event.seq}) 折叠 {folded} 条，"
                    f"但当时只有 {len(pairs)} 条对话消息（日志被改过？）"
                )
            pairs = [(event.seq, user(event.data["summary"]))] + pairs[folded:]
            continue
        message = _message_from_event(event)
        if message is not None:
            pairs.append((event.seq, message))
    return pairs


def sanitize_pairs(pairs: list[tuple[int, Message]]) -> list[tuple[int, Message]]:
    """修剪"配对不上"的消息（只删不造）——日志被截断时的兜底。

    规则（按顺序扫一遍）：

    - `tool` 消息：其 `tool_call_id` 必须在**前一条 assistant 消息声明过**的调用里
      （多条结果跟在同一条 assistant 后面是正常形态：assistant 一次申请多个调用）；
      配对不上 → 丢掉（孤儿结果）；
    - `assistant` 带 `tool_calls`：若其中某个调用**始终没有结果**（日志在结果落盘前
      断了），把那些调用从这条消息里摘掉；摘空且没有正文 → 整条丢掉。

    为什么是"删"而不是"补一条结果"：补 = 往模型的历史里塞进**日志里没有的内容**，
    违反"模型可见即已记录"（铁律 #1）。删只影响投影（日志原样留着），而且
    "发生过什么"仍能从日志里查（`assistant/message` 的 tool_calls 还在日志里）。
    """
    cleaned: list[tuple[int, Message]] = []
    #: 最近一条带 tool_calls 的 assistant 消息：下标 + 还在等结果的调用 id
    pending: dict[str, Any] | None = None
    for seq, message in pairs:
        if message.role == "assistant":
            if pending is not None:
                cleaned = _drop_unanswered(cleaned, pending)     # 上一条：收尾
            ids = [call.id for call in message.tool_calls]
            pending = {"index": len(cleaned), "ids": set(ids)} if ids else None
            cleaned.append((seq, message))
            continue
        if message.role == "tool":
            if pending is not None and message.tool_call_id in pending["ids"]:
                pending["ids"].discard(message.tool_call_id)
                cleaned.append((seq, message))
            # 孤儿结果（对应的申请不在投影里）：丢掉
            continue
        # user / system：上一轮对话就此结束，未应答的调用在这里收尾
        if pending is not None:
            cleaned = _drop_unanswered(cleaned, pending)
            pending = None
        cleaned.append((seq, message))
    if pending is not None:
        cleaned = _drop_unanswered(cleaned, pending)
    return cleaned


def _drop_unanswered(
    pairs: list[tuple[int, Message]], pending: dict[str, Any]
) -> list[tuple[int, Message]]:
    """把没人应答的那些 tool_calls 从对应的 assistant 消息里摘掉（见 sanitize_pairs）。"""
    if not pending["ids"]:
        return pairs
    index = pending["index"]
    if not 0 <= index < len(pairs):
        return pairs                     # 消息已被前一轮修剪掉：无事可做
    seq, message = pairs[index]
    kept_calls = [call for call in message.tool_calls if call.id not in pending["ids"]]
    if not kept_calls and not (message.content or "").strip():
        return pairs[:index] + pairs[index + 1 :]        # 空壳：整条丢
    trimmed = Message(
        role="assistant", content=message.content, tool_calls=kept_calls,
        tool_call_id=message.tool_call_id,
    )
    return pairs[:index] + [(seq, trimmed)] + pairs[index + 1 :]


def conversation_message_seqs(events: Iterable[Event]) -> list[int]:
    """当前投影出的对话消息各自的来源 seq（诊断/断言用；顺序即列表顺序）。"""
    return [seq for seq, _ in conversation_pairs(events)]


def derive_messages(events: Iterable[Event]) -> list[Message]:
    """从事件序列投影出模型历史；system 消息取"当前生效"的那一条。

    最后一步是 `sanitize_pairs`：日志被截断/损坏时，投影仍要产出一份**能被 API
    接受**的历史（否则那条会话会被永久卡死——每次请求都 400）。日志本身不动。
    """
    materialized = list(events)
    messages = [message for _, message in sanitize_pairs(conversation_pairs(materialized))]
    text = effective_system_prompt(materialized)
    if text is None:
        return messages
    if messages and messages[0].role == "system":
        messages[0] = system(text)
    else:
        messages.insert(0, system(text))
    return messages


__all__ = [
    "conversation_message_seqs",
    "conversation_pairs",
    "derive_messages",
    "effective_system_prompt",
    "latest_prompt_trace",
    "sanitize_pairs",
]
