"""事件模型 —— 会话日志的最小事实单元。

会话日志是 append-only 的事件序列：**每一条模型可见的输入、每一个生命周期
转折，都是一个事件**（铁律 #1：Model-visible means logged）。模型历史不直接
存储，而是由这些事件"投影"出来（见 projection.py）。

本模块只定义事件长什么样、有哪些类型、写入前怎么校验；不涉及投影、不涉及落盘。

对应 dsh：`packages/core/session/src/types.ts`（事件词汇表）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# 会话事件类型（本练习的最小集合）。
#   session/start     会话建立（携带 system 提示等初始配置）
#   turn/start|end    一次用户输入排空的开头 / 收尾
#   user/message      用户消息
#   step/start|end    一次模型往返的开头 / 收尾
#   assistant/message 模型回复（含工具申请）
#   tool/result       工具执行结果
EVENT_TYPES = frozenset(
    {
        "session/start",
        "turn/start",
        "turn/end",
        "user/message",
        "step/start",
        "step/end",
        "assistant/message",
        "tool/result",
    }
)

# 每种类型必须携带的字段（写入前校验，fail loud）。
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "session/start": ("session_id",),
    "turn/start": ("turn",),
    "turn/end": ("turn", "status"),
    "user/message": ("content",),
    "step/start": ("turn", "step"),
    "step/end": ("turn", "step"),
    "assistant/message": (),
    "tool/result": ("call_id", "name", "result"),
}


def now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（事件的写入时刻）。"""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Event:
    """一条会话事件：单调递增的 seq + 类型 + 负载 + 时间戳。

    frozen 阻止属性重绑定，但要真正防住"改负载"，靠的是 Session/store 在
    进出的两端做深拷贝 —— dataclass 的 frozen 管不住里面那个 dict。
    """

    seq: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 JSONL 的字典。"""
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Event:
        """从 JSONL 反序列化。"""
        return cls(
            seq=int(raw["seq"]),
            type=str(raw["type"]),
            data=raw.get("data", {}),
            ts=str(raw.get("ts", "")),
        )


def validate(event: Event) -> None:
    """写入前校验：类型已知 + 必填字段齐全。违规直接抛错（fail loud）。"""
    if event.type not in EVENT_TYPES:
        raise ValueError(f"未知事件类型：{event.type}（合法类型见 EVENT_TYPES）")
    missing = [key for key in _REQUIRED_FIELDS[event.type] if key not in event.data]
    if missing:
        raise ValueError(f"事件 {event.type} 缺少必填字段：{missing}")
    if event.type == "assistant/message":
        for call in event.data.get("tool_calls", []):
            for key in ("id", "name"):
                if key not in call:
                    raise ValueError(f"assistant/message 的 tool_call 缺少字段：{key}")


def copy_data(data: dict[str, Any]) -> dict[str, Any]:
    """深拷贝事件负载，用于进/出日志时的隔离。"""
    return deepcopy(data)
