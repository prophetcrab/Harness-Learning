"""ContextSession —— 在冻结的基线 Session 上扩展"事件词汇"与"投影"。

为什么需要子类：本阶段要求"每 step 渲染的系统提示词写进日志"——即新增一类事件
`system/message`。而基线的 `Session.append` 按一张**封闭**的事件类型表做校验
（未知类型 fail loud），且 P2 约定 `harness/` 冻结不动。于是扩展发生在阶段顶层，
用一个薄子类收敛：

- **append**：基线已知的类型原样走 `super()`（纪律完全不变）；只有本阶段新增的
  类型走一条同构的写入路径（校验必填字段 → 深拷贝 → 落盘 → 入内存 → seq 递增）。
- **derive_messages**：改用扩展投影 `project()`——最近的 system/message 遮蔽
  早先文本（日志只增不改，遮蔽发生在派生时）。

代价（诚实记录）：新增类型的写入路径对基线的私有状态有依赖（`_store` / `_events`
/ `_copy_event`），且是十来行的"同构复刻"。之所以可以接受：扩展面只有一个事件
类型、一条路径；若未来阶段还要添加更多事件类型，应回头给基线评审一个"事件类型
注册"接缝（见 docs/decisions/0007 的"后果"一节）。
"""

from __future__ import annotations

from typing import Any

from context.projection import project
from harness.llm.vocabulary import Message
from harness.session import Event, Session
from harness.session.events import copy_data, now_iso

# 本阶段在基线事件表之外新增的类型 → 必填字段。
EXTRA_EVENT_TYPES: dict[str, tuple[str, ...]] = {
    "system/message": ("content",),
}


class ContextSession(Session):
    """带 system/message 事件词汇的会话日志（其余行为与基线完全一致）。"""

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        if event_type not in EXTRA_EVENT_TYPES:
            return super().append(event_type, data)

        payload = dict(data or {})
        missing = [key for key in EXTRA_EVENT_TYPES[event_type] if key not in payload]
        if missing:
            raise ValueError(f"事件 {event_type} 缺少必填字段：{missing}")
        # 与基线 Session.append 同构的写入路径（不经过基线的事件类型表）。
        event = Event(
            seq=self.last_seq + 1,
            type=event_type,
            data=copy_data(payload),
            ts=now_iso(),
        )
        if self._store is not None:
            self._store.append(self.session_id, event)
        self._events.append(event)
        self._seq = event.seq
        return self._copy_event(event)

    def derive_messages(self) -> list[Message]:
        """投影：system/message 逐条遮蔽，最近一次生效（见 context.projection）。"""
        return project(self._events)


__all__ = ["ContextSession", "EXTRA_EVENT_TYPES"]
