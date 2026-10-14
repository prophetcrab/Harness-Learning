"""会话日志构件 —— 事件溯源（append-only 事件 + JSONL 落盘 + 投影）。

P1 `_04` 的心脏，原样沿用；P2 的 `system/message` 遮蔽已合并进投影
（见 projection.py 顶部说明）。
"""

from components.session.events import EVENT_TYPES, Event, copy_data, now_iso, validate
from components.session.log import Session
from components.session.projection import (
    derive_messages,
    effective_system_prompt,
    latest_prompt_trace,
)
from components.session.store import JsonlStore, RepairReport

__all__ = [
    "EVENT_TYPES",
    "Event",
    "JsonlStore",
    "RepairReport",
    "Session",
    "copy_data",
    "derive_messages",
    "effective_system_prompt",
    "latest_prompt_trace",
    "now_iso",
    "validate",
]
