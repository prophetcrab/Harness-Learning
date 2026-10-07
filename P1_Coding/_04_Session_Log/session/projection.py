"""投影 —— 把事件日志投影成"模型历史"（derive_messages）。

这是本练习最重要的一条纪律：**消息永远由日志派生，绝不直接存储**（铁律 #2
的反面：日志是唯一真相，模型历史只是它的一个视图）。

好处：
- 日志是 append-only 的事实，历史是它的纯函数结果 → 重放必然同构；
- 想换一种"给模型看什么"的策略（压缩、遮蔽、裁剪），改投影即可，日志不动。

对应 dsh：`packages/core/session/src/surface.ts`（消息投影）。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable

from llm_seam.vocabulary import Message, ToolCall, assistant, system, tool_result, user

from session.events import Event


def derive_messages(events: Iterable[Event]) -> list[Message]:
    """从事件序列投影出模型历史。

    映射关系（只有这四类事件参与消息投影）：
        session/start     → system 消息（若有 system_prompt）
        user/message      → user 消息
        assistant/message → assistant 消息（含 tool_calls）
        tool/result       → tool 消息（tool_call_id 与申请配对）

    生命周期事件（turn/step 的 start/end）不产生消息，只用于观测与恢复边界。
    """
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
