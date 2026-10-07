"""session —— _04 练习的包：append-only 会话日志 + JSONL 落盘 + 投影。

本包把 _02/_03 那个"只活在内存里的轨迹"升级成**事件溯源**的会话日志：

    事件日志（唯一真相） ──投影──▶ 模型历史（视图）
    （append-only，可落盘）        （derive_messages，绝不直接存储）

文件分工（推荐阅读顺序）：

1. events.py      事件模型：Event + 事件类型 + 校验（唯一的"事实"定义）
2. projection.py  derive_messages：日志 → 模型历史（纯函数）
3. log.py         Session：单调 seq、深拷冻结、append-only
4. store.py       JsonlStore：<root>/<id>/session.jsonl；崩溃尾部修复；list
5. recorder.py    SessionRecorder：AgentLoop 事件流 → 会话事件

对应 dsh：`packages/core/session/`（types/index/surface）+
`packages/session/session-persistence-jsonl/`。
"""

from session.events import EVENT_TYPES, Event, validate
from session.log import Session
from session.projection import derive_messages
from session.recorder import SessionRecorder
from session.store import JsonlStore, RepairReport

__all__ = [
    "Event",
    "EVENT_TYPES",
    "validate",
    "derive_messages",
    "Session",
    "JsonlStore",
    "RepairReport",
    "SessionRecorder",
]
