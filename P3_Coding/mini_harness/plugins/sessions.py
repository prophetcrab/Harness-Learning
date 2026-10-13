"""sessions 插件 —— 会话日志存储（`sessions` 服务）。

把 P1 `_04` 的会话日志构件包装成服务：打开（或恢复）一条会话。**打开即恢复**
——同一个 `open(id)` 对新会话是创建、对已有会话是装载后继续，没有单独的恢复
分支（P1 定下的语义，此处原样沿用）。

服务接口（agent 插件只认它）：

    open(session_id, system_prompt, prompt_trace=None) -> Session
        装载日志；若是新会话则写 session/start（携带初始渲染与装配单）。
    load(session_id) -> (events, RepairReport)
        只读装载（含崩溃尾部修复报告）——用于重放/分叉/观测。
    list_sessions() -> list[str]
    fork(src, new, upto_seq=None) -> Session

崩溃尾部修复、append-only、fsync 语义都在构件层（components/session/store.py），
本插件不重复实现，只负责把它接进 ctx 并广播 `session/opened`。
"""

from __future__ import annotations

from pathlib import Path

from components.session import JsonlStore, RepairReport, Session
from kernel import Context
from plugins.events import SESSION_OPENED
from plugins.services import SESSIONS_SERVICE


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
        session = Session(session_id, store=self._store, events=events)
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

    def load(self, session_id: str) -> tuple[list, RepairReport]:
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

        forked = Session(new_id, store=self._store, events=[])
        for event in events:
            forked.append(event.type, event.data)
        return forked


class SessionsPlugin:
    """提供 `sessions` 服务（会话日志根目录在构造时定死）。"""

    name = "sessions"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def setup(self, ctx: Context) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        ctx.provide(SESSIONS_SERVICE, SessionsService(self._root, ctx))


__all__ = ["SessionsPlugin", "SessionsService"]
