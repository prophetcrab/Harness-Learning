"""context —— 上下文计量的构件层（估算与构成拆分）。

给两处用：`plugins/context_meter.py`（仪表面板）与
`components/agent/compact.py`（压缩的触发与预算）。
"""

from components.context.tokens import (
    DEFAULT_CONTEXT_WINDOW,
    MESSAGE_OVERHEAD,
    PART_ORDER,
    REQUEST_OVERHEAD,
    as_int,
    estimate_tokens,
    measure_parts,
    message_tokens,
    request_tokens,
    serialize_message,
)

__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "MESSAGE_OVERHEAD",
    "PART_ORDER",
    "REQUEST_OVERHEAD",
    "as_int",
    "estimate_tokens",
    "measure_parts",
    "message_tokens",
    "request_tokens",
    "serialize_message",
]
