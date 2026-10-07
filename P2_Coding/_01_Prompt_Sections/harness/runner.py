"""会话运行器 —— 把 Session / JsonlStore / AgentLoop 粘起来。

这层是"胶水"：它负责组装一次可运行的会话（打开或恢复），并把主循环的事件流
接进会话日志。它是 demo 和 cli 唯一需要打交道的入口。

关键纪律（事件溯源的落地）：
- **system 提示进日志**：`open_session` 把 system_prompt 写成 `session/start` 事件；
  循环的历史一律由 `derive_messages()` 从日志投影，不再由循环参数注入。
- **打开即恢复**：同一个 `open_session(id)` 对新会话是"创建"，对已存在的会话是
  "恢复"——两者路径完全一样（读日志 → 投影 → 建循环），不存在专门的恢复分支。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from harness.agent import AgentLoop, ExecuteTool, TurnResult
from harness.llm.provider import LLMProvider
from harness.llm.vocabulary import Message
from harness.session.log import Session
from harness.session.recorder import SessionRecorder
from harness.session.store import JsonlStore, RepairReport

EventSink = Callable[[str, dict], None]


def open_session(
    session_id: str,
    store: JsonlStore | None = None,
    *,
    system_prompt: str = "",
) -> tuple[Session, RepairReport]:
    """打开（或创建）一个会话：从 store 装载事件，若尚未开始则写 session/start。

    返回 (Session, RepairReport)。report.repaired 为真表示上次会话被强杀、
    尾部已自动修复。
    """
    if store is not None:
        events, report = store.load(session_id)
    else:
        events, report = [], RepairReport()
    session = Session(session_id, store=store, events=events)
    if not session.has_started:
        session.append(
            "session/start",
            {"session_id": session_id, "system_prompt": system_prompt},
        )
    return session, report


class Runner:
    """把一条会话跑起来：持有 Session 与 AgentLoop，事件自动落日志。"""

    def __init__(
        self,
        session: Session,
        provider: LLMProvider,
        execute_tool: ExecuteTool,
        tools: Sequence,
        *,
        max_steps: int = 8,
        on_event: EventSink | None = None,
    ) -> None:
        self.session = session
        self._tools = list(tools)
        self._provider = provider
        self._execute_tool = execute_tool
        self._max_steps = max_steps
        self._presenter = on_event
        self._loop = self._build_loop()

    def _build_loop(self) -> AgentLoop:
        recorder = SessionRecorder(self.session)

        def sink(kind: str, payload: dict) -> None:
            recorder(kind, payload)          # 先落日志（唯一真相）
            if self._presenter is not None:
                self._presenter(kind, payload)  # 再交给观察者（打印等）

        initial_messages = self.session.derive_messages()
        return AgentLoop(
            self._provider,
            self._execute_tool,
            max_steps=self._max_steps,
            on_event=sink,
            initial_messages=initial_messages,
            initial_turn=self.session.last_turn_number,
        )

    def send(self, user_text: str) -> TurnResult:
        """处理一个 turn：把一条用户输入排空。事件由 sink 自动落日志。"""
        return self._loop.run(user_text, tools=self._tools)

    @property
    def messages(self) -> list[Message]:
        """当前模型历史（由日志投影而来）。"""
        return self.session.derive_messages()

    @property
    def history(self) -> list[Message]:
        """主循环在内存里持有的历史（在线执行的真实结果，用于同构对照）。"""
        return self._loop.history


def load_messages(session_id: str, store: JsonlStore) -> list[Message]:
    """只从磁盘日志投影出模型历史（不需要构造 Runner）。"""
    events, _ = store.load(session_id)
    session = Session(session_id, store=None, events=events)
    return session.derive_messages()


def fork_session(
    store: JsonlStore,
    session_id: str,
    new_id: str,
    *,
    upto_seq: int | None = None,
) -> Session:
    """从已有会话分叉出新会话：拷贝 [1..upto_seq] 的事件到 new_id。

    upto_seq 为 None 时拷贝全部。new_id 已存在则报错（fail loud，不覆盖）。
    这是"日志只增不改"的延伸：分叉 = 在新日志里重放旧前缀，原日志不动。
    """
    if store.exists(new_id):
        raise FileExistsError(f"目标会话已存在，拒绝覆盖：{new_id}")
    events, _ = store.load(session_id)
    if upto_seq is not None:
        events = [event for event in events if event.seq <= upto_seq]

    forked = Session(new_id, store=store, events=[])
    for event in events:
        forked.append(event.type, event.data)
    return forked


def session_root(path: str | Path) -> JsonlStore:
    """便捷构造：给定根目录返回一个 JsonlStore。"""
    return JsonlStore(Path(path))
