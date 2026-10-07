"""Session —— 会话日志本体：append-only 事件序列（内存态 + 可选落盘）。

三条纪律（对应铁律 #1/#2）：
1. **只增不改**：只有 append，没有 update/delete；seq 单调递增。
2. **写入即冻结**：进日志的事件负载深拷贝一份，调用方之后怎么改都不影响日志；
   读出来的也是拷贝，防止外部改坏内部事实。
3. **写入前校验**：非法事件（未知类型/缺字段）当场报错，不留到读取时才炸。

落盘交给 store（可选）；Session 本身不关心存到哪、怎么存。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from harness.llm.vocabulary import Message
from harness.session.events import Event, copy_data, now_iso, validate
from harness.session.projection import derive_messages


class Session:
    """一个会话的事件日志。"""

    def __init__(
        self,
        session_id: str,
        *,
        store: Any | None = None,
        events: Iterable[Event] | None = None,
    ) -> None:
        self._id = session_id
        self._store = store
        # 装载已有事件（resume/load 时传入）；深拷贝隔离外部。
        self._events: list[Event] = [self._copy_event(e) for e in (events or [])]
        self._seq = self._events[-1].seq if self._events else 0

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._id

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def events(self) -> list[Event]:
        """全部事件（深拷贝，外部改动不影响日志）。"""
        return [self._copy_event(e) for e in self._events]

    @property
    def has_started(self) -> bool:
        """会话是否已经写入 session/start。"""
        return any(e.type == "session/start" for e in self._events)

    @property
    def last_turn_number(self) -> int:
        """已发生过的最大 turn 编号（没有则为 0）。"""
        turns = [e.data["turn"] for e in self._events if e.type == "turn/start"]
        return max(turns) if turns else 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        """追加一条事件：分配 seq → 深拷贝负载 → 校验 → 落盘 → 入内存。"""
        event = Event(
            seq=self._seq + 1,
            type=event_type,
            data=copy_data(data or {}),
            ts=now_iso(),
        )
        validate(event)  # 校验失败直接抛错，内存与磁盘都不会被污染
        if self._store is not None:
            self._store.append(self._id, event)
        self._events.append(event)
        self._seq = event.seq
        return self._copy_event(event)

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------

    def derive_messages(self) -> list[Message]:
        """把事件日志投影成模型历史。"""
        return derive_messages(self._events)

    # ------------------------------------------------------------------

    @staticmethod
    def _copy_event(event: Event) -> Event:
        """事件深拷贝（隔离进出两端的负载 dict）。"""
        return Event(
            seq=event.seq,
            type=event.type,
            data=copy_data(event.data),
            ts=event.ts,
        )
