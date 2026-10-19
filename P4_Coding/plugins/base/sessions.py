"""sessions —— 会话内核（基础插件）。

提供 `sessions` 服务：注册、追加事件、投影消息历史、打开/恢复的入口。

**唯一写入口是 `append`**：所有事件都从这里出去，落盘 / 推流 / 统计都是订阅者。
`open(session_id)` 是"打开即恢复"——内存里没有就问持久化插件要历史
（通过事件总线上的 `session/load` 请求，避免 sessions 反向依赖存储插件）。

**事件形状**（所有插件共用这套词汇，靠字符串与字典约定，不靠共享代码）：

    session/start    system_prompt / prompt_trace / model
    turn/start       turn / text
    user/message     text / source（steer 或 inject）
    step/start       turn / step
    assistant        content / tool_calls[{id,name,arguments}]
    tool/call        call_id / name / arguments
    tool/result      call_id / name / status / content / message
    step/end         turn / step / usage
    turn/end         turn / status / steps / final_text
    system/message   content / prompt_trace
    compact/checkpoint

**边界**：广播与加载用**字典**（`event.to_dict()`），这样持久化插件不需要
import 本模块的类型；投影出的消息是本地 `Message` / `ToolCall`（鸭子类型，
字段与 llm 的副本一致）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core import Context

# ---------------------------------------------------------------------------
# 投影用的本地类型（与 llm 插件的同名类型字段一致；刻意不 import）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用（投影用）。"""

    id: str
    name: str
    arguments: str = "{}"


@dataclass(frozen=True)
class Message:
    """投影出的一条消息（鸭子类型：其它插件按属性读取即可）。"""

    role: str
    content: Any = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @property
    def text(self) -> str:
        return self.content if isinstance(self.content, str) else ""

    @property
    def images(self) -> tuple[Any, ...]:
        return ()

    @property
    def has_images(self) -> bool:
        return False


#: 会话服务的名字。
SESSIONS_SERVICE = "sessions"

#: 事件总线：每条会话事件都会广播（持久化 / 推流 / 统计订阅它）。
SESSION_EVENT = "session/event"

#: 请求式钩子：内存里没有这条会话时，向订阅者要历史。
#: handler 签名 `handler(session_id) -> list[SessionEvent] | None`（waterfall）。
SESSION_LOAD = "session/load"


class SessionsService:
    """会话注册表 + 事件广播。

    `open()` 的语义是"打开即恢复"（P1 语义）：内存里有就复用，没有就问
    `session/load` 订阅者要历史；谁都没有就创建一条新会话。
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._store = SessionStore()

    # ------------------------------------------------------------------
    # 打开 / 恢复
    # ------------------------------------------------------------------

    def open(
        self,
        session_id: str,
        *,
        system_prompt: str = "",
        prompt_trace: Mapping[str, Any] | None = None,
        model: Mapping[str, Any] | None = None,
        at: str = "",
    ) -> tuple[Any, bool]:
        """打开一条会话；返回 `(session, resumed)`。

        `resumed=True` 表示这条会话是从历史恢复的（此时不会重复写 session/start）；
        `False` 表示新建。

        **并发安全**：多个入口（并行客户端、同一进程的多个 agent）同时打开同一条
        会话时，后到者不会因为"已经有人建好了"而报错——它返回先到者建的那条
        会话（`resumed=True`）。只有真正建出 `session/start` 的那一个返回 False。
        """
        existing = self._store.get(session_id)
        if existing is not None:
            return existing, True

        events = self._load(session_id)
        if events:
            try:
                session = self._store.restore(session_id, events)
            except ValueError:
                existing = self._store.get(session_id)
                if existing is not None:
                    return existing, True  # 另一个线程先恢复了，用它
                raise
            return session, True

        try:
            session = self._store.create(
                session_id,
                system_prompt=system_prompt,
                prompt_trace=prompt_trace,
                model=model,
                at=at,
            )
        except ValueError:
            existing = self._store.get(session_id)
            if existing is not None:
                return existing, True  # 另一个线程先建好了，用它
            raise
        self._broadcast(session_id, session.events[0])
        return session, False

    def _load(self, session_id: str) -> list[SessionEvent]:
        """问订阅者要历史（没有订阅者 / 没有历史 → 空列表）。"""
        found = self._ctx.waterfall(SESSION_LOAD, None, session_id=session_id)
        if not found:
            return []
        if not isinstance(found, list):
            raise TypeError(f"session/load 的返回值必须是事件列表或 None，收到：{found!r}")
        # 持久化插件返回的是字典（不 import 本模块的类型）。
        return [
            SessionEvent.from_dict(item) if isinstance(item, Mapping) else item
            for item in found
        ]

    # ------------------------------------------------------------------
    # 追加
    # ------------------------------------------------------------------

    def append(self, session_id: str, kind: str, payload: Mapping[str, Any], *, at: str = "") -> SessionEvent:
        """追加一条事件并广播（唯一写入口）。"""
        session = self._store.require(session_id)
        event = session.append(kind, payload, at=at)
        self._broadcast(session_id, event)
        return event

    def append_start(self, session_id: str) -> None:
        """补写 `session/start`（恢复路径不需要——历史里已经有了）。"""
        session = self._store.require(session_id)
        if session.events and session.events[0].kind == SESSION_START:
            return
        event = session.append(SESSION_START, {"system_prompt": "", "prompt_trace": {}, "model": {}})
        self._broadcast(session_id, event)

    def begin_turn(self, session_id: str, text: str, *, at: str = "") -> int:
        """原子地分配轮次号并写入 `turn/start`；返回轮次号。

        **为什么不是一个 `last_turn_number + 1`**：多个 agent 共用一个会话时，
        两个 runner 会同时读到同一个"最后轮次"并各写一条 `turn/start`——日志里
        出现重复轮次，排障时无法定位。分配与追加必须在同一次锁内完成。
        """
        session = self._store.require(session_id)
        event = session.begin_turn(text, at=at)
        self._broadcast(session_id, event)
        return int(event.payload["turn"])

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, session_id: str):
        return self._store.get(session_id)

    def require(self, session_id: str):
        return self._store.require(session_id)

    def messages(self, session_id: str):
        """投影出的消息历史（当前视图）。"""
        return self._store.require(session_id).messages()

    def events(self, session_id: str) -> tuple[SessionEvent, ...]:
        return self._store.require(session_id).events_snapshot()

    def system_prompt(self, session_id: str) -> str | None:
        return self._store.require(session_id).effective_system_prompt()

    @property
    def session_ids(self) -> tuple[str, ...]:
        return self._store.session_ids

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _broadcast(self, session_id: str, event: SessionEvent) -> None:
        # session_id 显式传：事件本身不带它（它是会话的属性），而订阅者
        # （落盘 / 推流 / 统计）都需要知道这条事件属于谁。
        self._ctx.emit(SESSION_EVENT, session_id=session_id, session_event=event.to_dict())


class SessionsPlugin:
    """提供 `sessions` 服务（会话内核）。装配单里应声明 `role="base"`。"""

    name = "sessions"

    def setup(self, ctx: Context) -> None:
        ctx.provide(SESSIONS_SERVICE, SessionsService(ctx))


def sessions_plugin(**config: Any) -> SessionsPlugin:
    """工厂（当前无配置——持久化目录属于 session-store-jsonl）。"""
    return SessionsPlugin(**config)


__all__ = [
    "ASSISTANT",
    "COMPACT_CHECKPOINT",
    "KNOWN_EVENT_TYPES",
    "SESSIONS_SERVICE",
    "SESSION_EVENT",
    "SESSION_LOAD",
    "SESSION_START",
    "STEP_END",
    "STEP_START",
    "SYSTEM_MESSAGE",
    "Session",
    "SessionEvent",
    "SessionStore",
    "SessionsPlugin",
    "SessionsService",
    "TOOL_CALL",
    "TOOL_RESULT",
    "TURN_END",
    "TURN_START",
    "USER_MESSAGE",
    "project_messages",
    "sessions_plugin",
]


# ======================================================================
# 以下合并自 components/session/store.py
# ======================================================================


#: 事件名（也是 JSONL 里的 `kind`）。
SESSION_START = "session/start"
SYSTEM_MESSAGE = "system/message"
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"
ASSISTANT = "assistant"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
COMPACT_CHECKPOINT = "compact/checkpoint"
#: 轮次中途注入的用户输入（steer / inject）：投影成一条 user 消息，
#: 但不算新的一轮（`turn/start` 才开启新轮）。
USER_MESSAGE = "user/message"

#: 本版本认识的**全部**事件类型。
#:
#: 读日志时遇到不认识的类型：带 `ignorable: true` 标记的跳过（向前兼容——
#: 那是新版本 harness 写的可选事件）；**没有标记就拒绝解读整份日志**，因为
#: 静默跳过一条必需事件会重建出一个错误的会话。
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        SESSION_START,
        SYSTEM_MESSAGE,
        TURN_START,
        TURN_END,
        STEP_START,
        STEP_END,
        ASSISTANT,
        TOOL_CALL,
        TOOL_RESULT,
        COMPACT_CHECKPOINT,
        USER_MESSAGE,
    }
)
class UnknownEventTypeError(ValueError):
    """读到本版本不认识、且没有 `ignorable` 标记的事件类型。"""


@dataclass(frozen=True)
class SessionEvent:
    """一条会话事件：序号 + 类型 + 数据 + 时间戳（+ 可选的忽略标记）。"""

    seq: int
    kind: str
    payload: Mapping[str, Any]
    at: str  # ISO 8601（UTC）
    #: 写日志的一方声明"不认识我时可以安全跳过"。只对**本版本不认识**的类型
    #: 有意义；认识的类型一律正常解读。
    ignorable: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "seq": self.seq,
            "kind": self.kind,
            "payload": dict(self.payload),
            "at": self.at,
        }
        if self.ignorable:
            data["ignorable"] = True
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionEvent:
        seq = data.get("seq")
        kind = data.get("kind")
        if not isinstance(seq, int) or not isinstance(kind, str):
            raise ValueError(f"事件缺少 seq/kind：{dict(data)!r}")
        payload = data.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ValueError(f"事件 {kind!r} 的 payload 必须是字典：{payload!r}")
        ignorable = bool(data.get("ignorable", False))
        if kind not in KNOWN_EVENT_TYPES and not ignorable:
            raise UnknownEventTypeError(
                f"未知事件类型：{kind!r}（没有 ignorable 标记——"
                "它可能是更新版本的 harness 写的必需事件，拒绝解读以免重建出错误会话）"
            )
        return cls(
            seq=seq,
            kind=kind,
            payload=payload,
            at=str(data.get("at", "")),
            ignorable=ignorable,


        )
@dataclass
class Session:
    """一条会话：只增不改的事件列表 + 投影缓存。"""

    session_id: str
    events: list[SessionEvent] = field(default_factory=list)
    #: 投影结果缓存（事件追加时失效）。
    _messages: list[Message] | None = field(default=None, repr=False)
    #: 同一会话的追加可能来自多个线程（多 agent / 工具线程）。`last_seq + 1`
    #: 是读-改-写，没有锁会产生重复 seq——落盘后整份日志都不可读。
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self.events[-1].seq if self.events else 0

    @property
    def last_turn_number(self) -> int:
        """最近一轮的序号（turn/start 的 payload 里带 turn）。"""
        with self._lock:
            for event in reversed(self.events):
                if event.kind == TURN_START:
                    return int(event.payload.get("turn", 0))
            return 0

    def append(self, kind: str, payload: Mapping[str, Any], *, at: str = "") -> SessionEvent:
        """追加一条事件（序号自增）；投影缓存立刻失效。

        `at` 缺省取**当前 UTC 时间**（ISO 8601）。时间戳是日志排障与统计的
        基础，不能留空——之前的实现默认空串，是个实打实的缺失。
        """
        if not at:
            at = datetime.now(UTC).isoformat()
        with self._lock:
            event = SessionEvent(
                seq=self.last_seq + 1, kind=kind, payload=dict(payload), at=at
            )
            self.events.append(event)
            self._messages = None
            return event

    def extend(self, events: list[SessionEvent]) -> None:
        """批量追加已有事件（恢复时用）；序号必须严格递增。"""
        with self._lock:
            for event in events:
                if event.seq <= self.last_seq:
                    raise ValueError(
                        f"事件序号必须递增：{event.seq} 不大于 {self.last_seq}"
                    )
                self.events.append(event)
            self._messages = None

    def begin_turn(self, text: str, *, at: str = "") -> SessionEvent:
        """原子地分配轮次号并追加 `turn/start`（返回写入的事件）。"""
        if not at:
            at = datetime.now(UTC).isoformat()
        with self._lock:
            number = 0
            for event in reversed(self.events):
                if event.kind == TURN_START:
                    number = int(event.payload.get("turn", 0))
                    break
            event = SessionEvent(
                seq=self.last_seq + 1,
                kind=TURN_START,
                payload={"turn": number + 1, "text": text},
                at=at,
            )
            self.events.append(event)
            self._messages = None
            return event

    def messages(self) -> list[Message]:
        """投影出消息历史（带缓存；事件变了才重算）。"""
        with self._lock:
            if self._messages is None:
                self._messages = project_messages(list(self.events))
            return list(self._messages)

    def events_snapshot(self) -> tuple[SessionEvent, ...]:
        """事件列表的线程安全快照（读方不必自己加锁）。"""
        with self._lock:
            return tuple(self.events)

    def effective_system_prompt(self) -> str | None:
        """当前生效的系统提示词（最后一个 system/message 或 session/start）。"""
        for event in reversed(self.events_snapshot()):
            if event.kind == SYSTEM_MESSAGE:
                text = event.payload.get("content")
                return str(text) if text is not None else None
            if event.kind == SESSION_START:
                text = event.payload.get("system_prompt")
                return str(text) if text is not None else None
        return None


def project_messages(events: list[SessionEvent]) -> list[Message]:
    """把事件流投影成消息历史（纯函数，便于测试与重放）。

    **系统提示词进入投影结果**：`session/start` 与后续的 `system/message` 都是
    提示词的载体（后者只在提示词指纹变化时追加）。模型每次请求只应看到当前
    生效的那一版，所以取最后一段、放在消息列表开头——而不是把它留在原地
    （那会让 system 消息出现在历史中间）。
    """
    messages: list[Message] = []
    system_prompt: str | None = None
    pending_calls: dict[str, ToolCall] = {}

    for event in events:
        payload = event.payload
        if event.kind == SESSION_START:
            text = payload.get("system_prompt")
            if text is not None:
                system_prompt = str(text)
        elif event.kind == SYSTEM_MESSAGE:
            text = payload.get("content")
            if text is not None:
                system_prompt = str(text)
        elif event.kind == TURN_START:
            text = payload.get("text")
            if text is not None:
                messages.append(Message(role="user", content=str(text)))
        elif event.kind == USER_MESSAGE:
            text = payload.get("text")
            if text is not None:
                messages.append(Message(role="user", content=str(text)))
        elif event.kind == ASSISTANT:
            content = payload.get("content") or ""
            calls = tuple(
                ToolCall(
                    id=str(call.get("id")),
                    name=str(call.get("name")),
                    arguments=str(call.get("arguments", "{}")),
                )
                for call in payload.get("tool_calls") or ()
            )
            for call in calls:
                pending_calls[call.id] = call
            messages.append(
                Message(role="assistant", content=str(content), tool_calls=calls)
            )
        elif event.kind == TOOL_RESULT:
            call_id = str(payload.get("call_id", ""))
            content = payload.get("content")
            # 失败 / 拒绝 / 超时的工具没有 content，真正的解释在 message 里。
            # 只投影 content 会让模型看到字符串 "null"，等于没拿到错误原因。
            if isinstance(content, str) and content:
                text = content
            elif content is not None:
                text = json.dumps(content, ensure_ascii=False, default=str)
            else:
                text = str(payload.get("message") or "")
            status = payload.get("status")
            if status and status != "ok":
                text = f"[{status}] {text}"
            messages.append(
                Message(role="tool", content=text, tool_call_id=call_id)
            )
            pending_calls.pop(call_id, None)
    if system_prompt:
        messages.insert(0, Message(role="system", content=system_prompt))
    return messages


class SessionStore:
    """会话注册表：创建 / 恢复 / 查找。

    线程安全：同一条会话的追加在锁内（JSONL 落盘插件也从这里读事件）。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def create(
        self,
        session_id: str,
        *,
        system_prompt: str = "",
        prompt_trace: Mapping[str, Any] | None = None,
        model: Mapping[str, Any] | None = None,
        at: str = "",
    ) -> Session:
        """创建一条新会话并写入 `session/start`；同名已存在 → ValueError。"""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session_id 必须是非空字符串：{session_id!r}")
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"会话已存在：{session_id!r}（要恢复请用 open）")
            session = Session(session_id=session_id)
            session.append(
                SESSION_START,
                {
                    "system_prompt": system_prompt,
                    "prompt_trace": dict(prompt_trace or {}),
                    "model": dict(model or {}),
                },
                at=at,
            )
            self._sessions[session_id] = session
            return session

    def restore(self, session_id: str, events: list[SessionEvent]) -> Session:
        """用已有事件恢复（注册表里没有才能恢复；已存在 → ValueError）。"""
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"会话已在内存里：{session_id!r}")
            session = Session(session_id=session_id)
            session.extend(events)
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def require(self, session_id: str) -> Session:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"会话不存在：{session_id!r}（先 open / restore）")
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            if self._sessions.pop(session_id, None) is None:
                raise KeyError(f"会话不存在：{session_id!r}")

    @property
    def session_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._sessions)
