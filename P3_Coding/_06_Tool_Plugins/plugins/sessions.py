"""sessions 插件 —— 会话日志存储（`sessions` 服务）。

把 P1 `_04` 的会话日志构件包装成服务：打开（或恢复）一条会话。**打开即恢复**
——同一个 `open(id)` 对新会话是创建、对已有会话是装载后继续，没有单独的恢复
分支（P1 定下的语义，此处原样沿用）。

服务接口（agent / rpc 插件只认它）：

    open(session_id, system_prompt, prompt_trace=None) -> Session
        装载日志；若是新会话则写 session/start（携带初始渲染与装配单）。
    load(session_id) -> (events, RepairReport)
        只读装载（含崩溃尾部修复报告）——用于重放/分叉/观测。
    list_sessions() -> list[str]
    fork(src, new, upto_seq=None) -> Session

**每条会话事件都发内核事件**（`session/event`）：本服务产出的 Session 是
`ObservableSession`——每次 `append` 成功后立刻 `ctx.emit("session/event", ...)`。
这就是 RPC `session.follow` 的推送源：推送的粒度是"每条会话事件"（长 turn
进行中也能看到事件流生长），而**真相仍在日志里**——重放读日志、订阅收事件，
两者是同一条记录的两个视图。

崩溃尾部修复、append-only 语义都在构件层（components/session/store.py），
本插件不重复实现，只负责把它接进 ctx 并广播 `session/opened` / `session/event`。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from components.session import Event, JsonlStore, RepairReport, Session
from kernel import Context
from plugins.events import SESSION_EVENT, SESSION_OPENED
from plugins.services import SESSIONS_SERVICE


class ObservableSession(Session):
    """Session + append 时回调（本服务用它发 `session/event` 内核事件）。

    只加一个回调、不改任何写入路径：先走基线 `append`（校验/落盘/入内存），
    成功后再回调——事件流看到的永远是**已确认落盘**的事实。
    """

    def __init__(
        self,
        session_id: str,
        *,
        store: Any | None = None,
        events: Any | None = None,
        on_append: Callable[[Event], None] | None = None,
    ) -> None:
        super().__init__(session_id, store=store, events=events)
        self._on_append = on_append

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        event = super().append(event_type, data)
        if self._on_append is not None:
            self._on_append(event)
        return event


class SessionsService:
    """会话日志的打开/恢复/列举/分叉（一个 JsonlStore 之上的薄服务）。"""

    def __init__(self, root: str | Path, ctx: Context | None = None) -> None:
        self._store = JsonlStore(Path(root))
        self._ctx = ctx

    @property
    def store(self) -> JsonlStore:
        return self._store

    @property
    def root(self) -> Path:
        return self._store.root

    # ------------------------------------------------------------------
    # 内部：产出带事件广播的会话
    # ------------------------------------------------------------------

    def _make_session(self, session_id: str, events: list[Event]) -> Session:
        on_append: Callable[[Event], None] | None = None
        if self._ctx is not None:
            def on_append(event: Event) -> None:
                self._ctx.emit(SESSION_EVENT, session=session_id, record=event)

        return ObservableSession(
            session_id, store=self._store, events=events, on_append=on_append
        )

    # ------------------------------------------------------------------
    # 服务接口
    # ------------------------------------------------------------------

    def open(
        self,
        session_id: str,
        *,
        system_prompt: str = "",
        prompt_trace: dict | None = None,
    ) -> tuple[Session, bool, RepairReport]:
        """打开（或恢复）会话；返回 (会话, 是否新建, 修复报告)。

        - 已存在的会话：装载事件（顺带修复崩溃尾行），不重复写 session/start；
        - 新会话：写 session/start（system_prompt + 装配单），resumed=False。
        """
        events, report = self._store.load(session_id)
        session = self._make_session(session_id, events)
        resumed = session.has_started
        if not resumed:
            data: dict = {"session_id": session_id, "system_prompt": system_prompt}
            if prompt_trace is not None:
                data["prompt_trace"] = prompt_trace
            session.append("session/start", data)
        if self._ctx is not None:
            self._ctx.emit(
                SESSION_OPENED,
                session=session_id,
                resumed=resumed,
                repaired=report.repaired,
            )
        return session, resumed, report

    def load(self, session_id: str) -> tuple[list[Event], RepairReport]:
        """只读装载会话事件（不打开、不写任何东西）。"""
        return self._store.load(session_id)

    def list_sessions(self) -> list[str]:
        return self._store.list_sessions()

    def fork(self, source: str, new_id: str, *, upto_seq: int | None = None) -> Session:
        """从已有会话分叉：拷贝 [1..upto_seq] 的事件到 new_id（原日志不动）。"""
        if self._store.exists(new_id):
            raise FileExistsError(f"目标会话已存在，拒绝覆盖：{new_id}")
        events, _ = self._store.load(source)
        if upto_seq is not None:
            events = [event for event in events if event.seq <= upto_seq]

        forked = self._make_session(new_id, [])
        for event in events:
            forked.append(event.type, event.data)
        return forked


class SessionsPlugin:
    """提供 `sessions` 服务（会话日志根目录在构造时定死）。"""

    name = "sessions"
    #: 应用角色（base）：日志是唯一真相，没有它就没有会话。
    category = "base"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def setup(self, ctx: Context) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        ctx.provide(SESSIONS_SERVICE, SessionsService(self._root, ctx))


__all__ = ["ObservableSession", "SessionsPlugin", "SessionsService"]
