"""录制器 —— 把 AgentLoop 的事件流翻译成会话日志事件。

这是"模型可见 ⟺ 已记录"（铁律 #1）的落地点：AgentLoop 每发生一件事就 emit 一个
事件，本模块把它转成一条持久化事件写进 Session。翻译是**唯一的耦合点**——循环
不认识 Session，Session 也不认识循环，两边只通过事件名对接。

事件名映射（AgentLoop.emit → 会话事件类型）：

    turn_start    → turn/start + user/message
    step_request  → step/start
    step_response → assistant/message
    tool_result   → tool/result
    step_end      → step/end
    turn_end      → turn/end

未识别的事件名直接报错（fail loud）：循环改了事件契约却忘了同步录制器，要立刻
暴露，而不是静默漏记。
"""

from __future__ import annotations

from typing import Any

from session.log import Session


class SessionRecorder:
    """一个可调用对象，作为 AgentLoop 的 on_event 回调：事件 → 日志。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "turn_start":
            self._session.append("turn/start", {"turn": payload["turn"]})
            # 用户消息单独落一条 user/message —— 投影靠它产生 user 消息。
            self._session.append("user/message", {"content": payload["user"]})
        elif kind == "step_request":
            self._session.append(
                "step/start",
                {
                    "turn": payload["turn"],
                    "step": payload["step"],
                    "message_count": payload["message_count"],
                    "tool_count": payload["tool_count"],
                },
            )
        elif kind == "step_response":
            self._session.append(
                "assistant/message",
                {
                    "turn": payload["turn"],
                    "step": payload["step"],
                    "content": payload["content"],
                    "tool_calls": payload["tool_calls"],
                    "finish_reason": payload["finish_reason"],
                },
            )
        elif kind == "tool_result":
            self._session.append(
                "tool/result",
                {
                    "turn": payload["turn"],
                    "step": payload["step"],
                    "call_id": payload["call_id"],
                    "name": payload["name"],
                    "arguments": payload["arguments"],
                    "result": payload["result"],
                    "is_error": payload["is_error"],
                },
            )
        elif kind == "step_end":
            self._session.append("step/end", {"turn": payload["turn"], "step": payload["step"]})
        elif kind == "turn_end":
            self._session.append(
                "turn/end",
                {
                    "turn": payload["turn"],
                    "status": payload["status"],
                    "steps": payload["steps"],
                    "final_text": payload["final_text"],
                },
            )
        else:
            raise ValueError(f"录制器遇到未知事件：{kind}（AgentLoop 的事件契约变了？）")
