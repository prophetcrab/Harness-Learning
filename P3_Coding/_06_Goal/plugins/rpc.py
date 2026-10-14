"""rpc 插件 —— 把 harness 包成可被连接的 JSON-RPC 服务（stdio / TCP）。

P2 `_10`/`_11` 的服务化在 P3 底座上的重做：**服务端本身是一个插件**，只依赖
两个服务——`agent`（跑 turn）与 `sessions`（读日志做重放）。

    方法              语义
    initialize        握手：协议版本 / 服务器标识 / 能力清单（方法 + 工具面）
    session.prompt    跑一个 turn（{session, text} → 结果摘要）
    session.follow    订阅会话事件流：**先重放、再订阅**（P2 `_11` 的等式）

**follow = 重放 + 订阅**（本插件的心脏，与 P2 同一条纪律）：

- **重放**：从会话日志里取 `seq > from_seq` 的既有事件，逐条作为 `session.event`
  通知推给订阅方（顺序 = 日志顺序）；
- **订阅**：登记 `(session, last_seq, sender)`；此后该会话每产生一条新事件就
  立刻推一条通知——**推送粒度是"每条会话事件"**（长 turn 进行中也能看到事件流
  生长），来源是 `session/event` 内核事件（sessions 插件的 ObservableSession
  在 append 成功后发出）；
- **无缝衔接**：重放与登记在**同一把锁**内完成——重放结束时记下的 `last_seq`
  正是订阅起点，不重不漏。断线重连由客户端带 `from_seq` 补齐：**日志是唯一真相**，
  补缺不需要另建缓存，重放即可（这是事件溯源在服务化上的回报）。

两种运行方式：

- **stdio**：入口把 stdin/stdout 收作传输（`serve_stdio`，一行一帧；EOF 即断开）；
- **TCP**：`rpc_plugin(listen=port)` 装载期起服务（默认只听本机、多客户端共享同一
  会话），卸载即停服（端口释放）——与 webui 插件的生命周期语义一致。

线程边界（与 webui 同一套纪律）：生命周期只在主线程；连接线程只调服务；
**会话串行化**由 agent 服务自己的锁兜底（见 plugins/agent.py），本插件另加一把
锁保护"订阅台账 + 重放登记"的原子性与事件推送。

已知边界：无鉴权（默认只听本机）；无版本协商（握手只报版本，不做兼容分支）；
没有随附 attach 客户端（P2 有；在 P3 里它是独立小工具，属后续工作）。
"""

from __future__ import annotations

import io
import socketserver
import threading
from dataclasses import dataclass
from typing import Any

from components.rpc import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    LineTransport,
    RpcError,
    notification_frame,
)
from kernel import Context
from plugins.events import SESSION_EVENT
from plugins.services import AGENT_SERVICE, RPC_SERVICE, SESSIONS_SERVICE, TOOLS_SERVICE

#: 协议版本与服务器标识（initialize 响应的一部分）。
PROTOCOL_VERSION = "harness-jsonrpc/1"
SERVER_NAME = "p3-mini-harness"
SERVER_VERSION = "0.3.0"

#: 全部可用方法（写进 initialize 的能力清单，也是未知方法报错时的提示来源）。
METHODS = ("initialize", "session.prompt", "session.follow")

#: 事件通知的方法名（服务端主动推的帧，无 id）。
EVENT_NOTIFICATION = "session.event"


@dataclass
class Subscription:
    """一个事件流订阅：向哪个连接（sender）推哪个会话、从哪条之后继续推。"""

    token: int
    session: str
    sender: Any  # components.rpc.Channel（按对象身份比较）
    last_seq: int = 0
    pushed: int = 0


class RpcService:
    """RPC 方法面：initialize / session.prompt / session.follow（传输无关）。"""

    def __init__(self, ctx: Context, *, agent: Any, sessions: Any) -> None:
        self._ctx = ctx
        self._agent = agent
        self._sessions = sessions
        self._initialized = False
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._token = 0
        #: TCP 模式下的绑定地址（只读连接信息；stdio 模式保持 None）。
        self.address: tuple[str, int] | None = None
        # 订阅发生在装载期 → 注册即 effect：卸载 rpc 插件时自动退订。
        ctx.on(SESSION_EVENT, self._on_session_event)

    # ------------------------------------------------------------------
    # 分派
    # ------------------------------------------------------------------

    def dispatch(self, method: str, params: dict[str, Any], *, send: Any = None) -> Any:
        """按方法名分派；返回可序列化结果（错误以 RpcError 抛出）。"""
        if method == "initialize":
            return self.initialize(params)
        if method == "session.prompt":
            return self.session_prompt(params)
        if method == "session.follow":
            return self.session_follow(params, send=send)
        raise RpcError(METHOD_NOT_FOUND, f"未知方法：{method!r}（可用：{'、'.join(METHODS)}）")

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            raise RpcError(NOT_INITIALIZED, "服务尚未初始化：请先调用 initialize 完成握手")

    # ------------------------------------------------------------------
    # initialize：握手
    # ------------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手：返回协议版本、服务器标识与能力清单（重复握手幂等）。"""
        self._initialized = True
        toolbox = self._ctx.get(TOOLS_SERVICE)
        tools = sorted(spec.name for spec in toolbox.specs()) if toolbox is not None else []
        return {
            "protocol": PROTOCOL_VERSION,
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"methods": list(METHODS), "tools": tools},
        }

    # ------------------------------------------------------------------
    # session.prompt：跑一个 turn
    # ------------------------------------------------------------------

    def session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """跑一个 turn：`{"session": "s1", "text": "..."}` → 结果摘要。"""
        self._ensure_initialized()
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError(INVALID_PARAMS, "session.prompt 需要非空的 text 参数")
        session_id = params.get("session", "default")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RpcError(INVALID_PARAMS, "session 必须是非空字符串")

        result = self._agent.ask(text, session_id=session_id)
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

    def session_follow(self, params: dict[str, Any], *, send: Any) -> dict[str, Any]:
        """订阅会话事件流：重放 seq > from_seq 的既有事件，再登记实时推送。"""
        self._ensure_initialized()
        if send is None:
            raise RpcError(INVALID_PARAMS, "session.follow 需要可推送的连接通道")
        session_id = params.get("session", "default")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RpcError(INVALID_PARAMS, "session 必须是非空字符串")
        from_seq = params.get("from_seq", 0)
        if isinstance(from_seq, bool) or not isinstance(from_seq, int) or from_seq < 0:
            raise RpcError(INVALID_PARAMS, "from_seq 必须是 >= 0 的整数")

        with self._lock:
            events, _ = self._sessions.load(session_id)
            fresh = [event for event in events if event.seq > from_seq]
            for event in fresh:
                send(notification_frame(EVENT_NOTIFICATION, self._event_payload(session_id, event)))
            last_seq = fresh[-1].seq if fresh else from_seq
            self._token += 1
            self._subs.append(
                Subscription(
                    token=self._token,
                    session=session_id,
                    sender=send,
                    last_seq=last_seq,
                    pushed=len(fresh),
                )
            )
            return {"session": session_id, "replayed": len(fresh), "last_seq": last_seq}

    # ------------------------------------------------------------------
    # 推送与注销
    # ------------------------------------------------------------------

    @staticmethod
    def _event_payload(session_id: str, event: Any) -> dict[str, Any]:
        return {"session": session_id, **event.to_dict()}

    def _on_session_event(self, *, session: str, record: Any) -> None:
        """每条会话事件 → 推给所有匹配订阅（同锁内推进 last_seq，不重不漏）。"""
        with self._lock:
            for sub in self._subs:
                if sub.session == session and record.seq > sub.last_seq:
                    sub.sender(notification_frame(EVENT_NOTIFICATION, self._event_payload(session, record)))
                    sub.last_seq = record.seq
                    sub.pushed += 1

    def drop_sender(self, sender: Any) -> None:
        """连接断开：注销该连接登记过的全部订阅（订阅不泄漏）。"""
        with self._lock:
            self._subs = [sub for sub in self._subs if sub.sender is not sender]

    def close(self) -> None:
        """清空全部订阅（插件卸载时调用：台账是外部状态，归插件显式撤销）。"""
        with self._lock:
            self._subs = []

    @property
    def subscription_count(self) -> int:
        """当前订阅数（观测/测试用）。"""
        with self._lock:
            return len(self._subs)


# ---------------------------------------------------------------------------
# TCP 传输（stdio 见 serve_stdio）
# ---------------------------------------------------------------------------


class _RpcTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ConnectionHandler(socketserver.StreamRequestHandler):
    """一条 TCP 连接 = 一个 LineTransport（一行一帧，EOF 断开）。

    客户端强杀/断线在这里被正常收尾：EOF → `serve_forever` 的 finally 注销订阅；
    读写两侧的连接类异常一律吞掉（对端走了不是服务端错误，不该往日志里刷堆栈）。
    """

    def handle(self) -> None:
        reader = io.TextIOWrapper(self.rfile, encoding="utf-8", errors="replace", newline="\n")
        writer = io.TextIOWrapper(self.wfile, encoding="utf-8", newline="\n")
        try:
            LineTransport(reader, writer, self.server.rpc_service).serve_forever()  # type: ignore[attr-defined]
        except (ConnectionError, OSError, ValueError):
            # ConnectionResetError / BrokenPipeError / 底层已关闭等：客户端断线收尾。
            pass


def serve_stdio(service: RpcService, *, reader: Any = None, writer: Any = None) -> None:
    """把 stdin/stdout 当传输跑 stdio JSON-RPC（阻塞到 EOF）。"""
    import sys

    LineTransport(
        reader if reader is not None else sys.stdin,
        writer if writer is not None else sys.stdout,
        service,
    ).serve_forever()


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class RpcPlugin:
    """提供 `rpc` 服务；`listen` 给了就同时起 TCP 服务（装载即起、卸载即停）。"""

    name = "rpc"
    inject = (AGENT_SERVICE, SESSIONS_SERVICE)

    def __init__(self, *, listen: int | None = None, host: str = "127.0.0.1") -> None:
        self._listen = listen
        self._host = host

    def setup(self, ctx: Context):
        service = RpcService(
            ctx,
            agent=ctx.require(AGENT_SERVICE),
            sessions=ctx.require(SESSIONS_SERVICE),
        )
        ctx.provide(RPC_SERVICE, service)
        if self._listen is None:
            # stdio 模式：没有服务器要关，但订阅台账是外部状态，卸载时同样要清。
            return service.close

        server = _RpcTcpServer((self._host, self._listen), _ConnectionHandler)
        server.rpc_service = service  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, name="p3-rpc", daemon=True)
        thread.start()
        # port=0 时由系统分配空闲端口：真实地址以 server_address 为准。
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        service.address = (str(bound_host), int(bound_port))

        def dispose() -> None:
            server.shutdown()       # 停接收循环（阻塞至 serve_forever 退出）
            thread.join(timeout=5)  # 等线程收尾
            server.server_close()   # 关监听套接字 → 端口释放
            service.close()         # 台账是外部状态：清空全部订阅

        return dispose


def rpc_plugin(*, listen: int | None = None, host: str = "127.0.0.1") -> RpcPlugin:
    """工厂：`listen=None` 只提供 stdio 可用的服务；`listen=0` 由系统分配 TCP 端口。"""
    return RpcPlugin(listen=listen, host=host)


__all__ = [
    "EVENT_NOTIFICATION",
    "METHODS",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "RpcPlugin",
    "RpcService",
    "Subscription",
    "rpc_plugin",
    "serve_stdio",
]
