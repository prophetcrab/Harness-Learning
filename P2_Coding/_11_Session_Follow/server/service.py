"""服务层 —— HarnessService：把 harness 包成可被连接的常驻服务（含事件流订阅）。

M7 的方法面（`_11` 补上 follow）：

    initialize        握手：交换版本与能力清单（方法 + 工具名单）
    session.prompt    跑一个 turn（`{"session": "s1", "text": "..."}`）；会话按 id 缓存
    session.follow    订阅会话事件流：**先重放、再订阅**（`_11` 的核心机制）

**follow = 重放 + 订阅**（学习计划里那句等式的落地）：

- 重放：把日志里 `seq > from_seq` 的既有事件逐条作为 `session.event` 通知推给
  订阅方（顺序 = 日志顺序）；
- 订阅：登记 `(session, last_seq, sender)`；此后该会话每产生新事件，就作为
  新的 `session.event` 通知推给订阅方；
- **无缝衔接**：重放结束时的 `last_seq` 正是订阅起点——重放与实时之间不重不漏。
  这正是"日志是唯一真相"（铁律 #1）在服务化上的回报：断线期间产生的事件不需要
  任何缓存机制去"记住"，它们本来就在日志里，重连时按 seq 重放即可补齐。

**什么时候推**（本实现的推送时机，两条通路）：

1. **事件产生即推**：打开会话时挂 `on_event` 回调——主循环每记录一条会话事件，
   就调一次 `flush()`，把新事件推给订阅方。长 turn 进行中也能实时看到事件流
   （不只是 turn 结束才批量收到）。
2. **每次请求处理后兜底 flush**：任何 dispatch 结束都 flush 一遍（覆盖"没有
   on_event 的场景"与跨连接的情况——A 连接上的 prompt 产生的事件会推给
   B 连接上的订阅者）。

**多连接**：服务是被所有连接共享的单例（stdio 下只有一个连接；TCP 下多个），
dispatch 用可重入锁串行化；每个订阅记录它自己的 sender（往哪个连接写）。
连接断开时要调 `drop_sender` 注销它的订阅（transport 在 EOF 时做）。

三条设计（与 `_10` 一脉相承）：握手门禁（-32002）；审批 fail-closed；
无人工应答方默认拒绝。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context import collect_runtime_context, open_context_harness
from harness.tools import ApprovalPolicy
from harness.tools.approval import AutoDeny
from kernel import Context
from providers import LLM_CAPABILITY, TOOLBOX_CAPABILITY
from server.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    RpcError,
    notification_frame,
)

#: 协议版本与服务器标识（initialize 响应的一部分；golden 测试钉住）。
PROTOCOL_VERSION = "harness-jsonrpc/1"
SERVER_NAME = "mini-harness"
SERVER_VERSION = "0.2.0"

#: 全部可用方法（写进 initialize 的能力清单，也是未知方法报错时的提示来源）。
METHODS = ("initialize", "session.prompt", "session.follow")

#: 事件通知的方法名（服务端主动推的帧，无 id）。
EVENT_NOTIFICATION = "session.event"

#: sender：把一个帧（已序列化的文本行，不含换行）写到某个连接。
Sender = Callable[[str], None]


@dataclass
class Subscription:
    """一个事件流订阅：向哪个连接（sender）推哪个会话、从哪条之后继续推。"""

    token: int
    session_id: str
    sender: Sender
    last_seq: int = 0          # 已推到的最大 seq（重放结束时 = 日志最新 seq）
    pushed: int = field(default=0)  # 已推条数（观测/测试用）


class HarnessService:
    """基于一个已 boot 的 ctx 提供 RPC 方法（会话按需打开并缓存；支持多连接订阅）。"""

    def __init__(
        self,
        plug_ctx: Context,
        *,
        root: str | Path,
        workspace: str | Path | None = None,
        approval: ApprovalPolicy | None = None,
    ) -> None:
        self._ctx = plug_ctx
        self._root = Path(root)
        self._workspace = Path(workspace) if workspace is not None else Path(root)
        # 默认 fail-closed：服务端没有人工应答方，拒绝一切需审批操作。
        self._approval: ApprovalPolicy = approval or AutoDeny(
            "RPC 服务无交互应答方：未授权任何需审批操作（fail-closed）"
        )
        self._initialized = False
        self._sessions: dict[str, Any] = {}  # session id → ContextHarness
        self._subscriptions: list[Subscription] = []
        self._token_seq = 0
        # 可重入锁：dispatch 串行化（多连接下会话/订阅表是共享状态）；
        # flush 会在 dispatch 内部被调（on_event / 兜底），同线程重入，故用 RLock。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 方法分派
    # ------------------------------------------------------------------

    def dispatch(self, method: str, params: dict[str, Any], *, send: Sender | None = None) -> Any:
        """按方法名分派；返回可序列化的结果（服务层的错误以 RpcError 抛出）。

        send 是"往当前连接写一帧"的回调（transport 提供）：只有会主动推帧的方法
        （session.follow）需要它；方法结束后统一兜底 flush 一次订阅。
        """
        with self._lock:
            try:
                if method == "initialize":
                    return self.initialize(params)
                if method == "session.prompt":
                    return self.session_prompt(params)
                if method == "session.follow":
                    return self.session_follow(params, send=send)
                raise RpcError(
                    METHOD_NOT_FOUND, f"未知方法：{method!r}（可用：{'、'.join(METHODS)}）"
                )
            finally:
                self._flush()

    # ------------------------------------------------------------------
    # initialize：握手
    # ------------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手：返回协议版本、服务器标识与能力清单。

        params 暂无必需项（保留给客户端自报身份）；重复 initialize 幂等
        （返回同一份信息——服务端把"再握一次手"当作良性重试而不是错误）。
        """
        self._initialized = True
        tools = sorted(spec.name for spec in self._ctx.require(TOOLBOX_CAPABILITY).specs())
        return {
            "protocol": PROTOCOL_VERSION,
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"methods": list(METHODS), "tools": tools},
        }

    # ------------------------------------------------------------------
    # session.prompt：跑一个 turn
    # ------------------------------------------------------------------

    def session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """跑一个 turn：`{"session": "s1", "text": "..."}` → 结果摘要。

        校验（全部 fail loud，参数问题统一 INVALID_PARAMS）：
        - 未握手 → NOT_INITIALIZED；
        - `text` 必填且非空字符串；`session` 可选（默认 "default"）且必须是非空字符串。
        """
        if not self._initialized:
            raise RpcError(
                NOT_INITIALIZED, "服务尚未初始化：请先调用 initialize 完成握手"
            )
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError(INVALID_PARAMS, "session.prompt 需要非空的 text 参数")
        session_id = params.get("session", "default")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RpcError(INVALID_PARAMS, "session 必须是非空字符串")

        harness = self._open_session(session_id)
        result = harness.harness.send(text)
        return {
            "session": session_id,
            "turn": result.turn,
            "status": result.status,
            "steps": len(result.steps),
            "final_text": result.final_text,
        }

    # ------------------------------------------------------------------
    # session.follow：重放 + 订阅
    # ------------------------------------------------------------------

    def session_follow(self, params: dict[str, Any], *, send: Sender | None) -> dict[str, Any]:
        """订阅会话事件流：先重放 `seq > from_seq` 的既有事件，再挂实时订阅。

        参数：`{"session": "s1", "from_seq": 0}`（from_seq 可省，默认 0 = 从头）。
        - 重放的事件与之后实时推的事件，都是 `session.event` 通知：
          `{"jsonrpc":"2.0","method":"session.event","params":{"session":…,"event":{…}}}`；
        - 重放先于本方法的响应发出（客户端先收到数据、再收到确认）；
        - 响应告诉你 `replayed`（重放条数）与 `latest_seq`（订阅起点）——
          断线重连时把这个 `latest_seq` 存下来，下次 `from_seq` 用它即可补齐。

        会话不存在时按"打开即创建"处理（与 prompt 同语义）：新会话先落
        `session/start`，它也会被重放出来。
        """
        if not self._initialized:
            raise RpcError(
                NOT_INITIALIZED, "服务尚未初始化：请先调用 initialize 完成握手"
            )
        if send is None:
            raise RpcError(INVALID_PARAMS, "session.follow 需要可推送的连接（内部错误）")
        session_id = params.get("session", "default")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RpcError(INVALID_PARAMS, "session 必须是非空字符串")
        from_seq = params.get("from_seq", 0)
        if isinstance(from_seq, bool) or not isinstance(from_seq, int) or from_seq < 0:
            raise RpcError(INVALID_PARAMS, "from_seq 必须是非负整数")

        harness = self._open_session(session_id)
        events = harness.session.events
        replay = [event for event in events if event.seq > from_seq]
        for event in replay:
            send(self._event_frame(session_id, event))

        latest = events[-1].seq if events else 0
        self._token_seq += 1
        subscription = Subscription(
            token=self._token_seq, session_id=session_id, sender=send, last_seq=latest
        )
        subscription.pushed = len(replay)
        self._subscriptions.append(subscription)
        return {
            "session": session_id,
            "from_seq": from_seq,
            "replayed": len(replay),
            "latest_seq": latest,
            "subscription": subscription.token,
        }

    # ------------------------------------------------------------------
    # 订阅的推送与清理
    # ------------------------------------------------------------------

    @staticmethod
    def _event_frame(session_id: str, event: Any) -> str:
        return notification_frame(
            EVENT_NOTIFICATION, {"session": session_id, "event": event.to_dict()}
        )

    def _flush(self) -> None:
        """把所有订阅"还没推过"的新事件推出去（按 seq 顺序、逐条）。

        幂等：没有新事件时什么都不做。推的事件读自会话的**内存日志**
        （`session.events`）——它与落盘 JSONL 同源同序（每条 append 都先落盘）。
        """
        for subscription in self._subscriptions:
            harness = self._sessions.get(subscription.session_id)
            if harness is None:
                continue
            for event in harness.session.events:
                if event.seq <= subscription.last_seq:
                    continue
                subscription.sender(self._event_frame(subscription.session_id, event))
                subscription.last_seq = event.seq
                subscription.pushed += 1

    def drop_sender(self, sender: Sender) -> None:
        """注销某个连接的全部订阅（连接断开时由 transport 调用）。"""
        with self._lock:
            self._subscriptions = [
                subscription
                for subscription in self._subscriptions
                if subscription.sender != sender
            ]

    @property
    def subscriptions(self) -> list[Subscription]:
        """当前订阅列表（观测/测试用；不要就地改动）。"""
        return list(self._subscriptions)

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def _open_session(self, session_id: str):
        """同名复用缓存，新名新开一场（打开即恢复——日志语义在底层兜底）。

        挂 on_event 回调：主循环每记录一条会话事件就 flush 一次——
        订阅者因此能在长 turn **进行中**实时收到事件（而不是等 turn 结束）。
        """
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        llm = self._ctx.require(LLM_CAPABILITY)
        registry = self._ctx.require(TOOLBOX_CAPABILITY)
        harness = open_context_harness(
            session_id,
            provider=llm,
            root=self._root,
            workspace=self._workspace,
            approval=self._approval,
            context_source=lambda: collect_runtime_context(self._workspace),
            tool_registry=registry,
            on_event=lambda kind, payload: self._flush(),  # 事件产生即推
        )
        self._sessions[session_id] = harness
        return harness

    @property
    def open_sessions(self) -> list[str]:
        """当前缓存的会话 id（按打开顺序；观测/测试用）。"""
        return list(self._sessions)


__all__ = [
    "HarnessService",
    "METHODS",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "EVENT_NOTIFICATION",
    "Subscription",
    "Sender",
]
