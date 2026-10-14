"""webui 插件 —— 浏览器界面：装载即起服务，卸载即停服（界面也是一个插件）。

这个插件回答的是"界面能不能也是插件"：能，而且它比多数能力**更**需要插件语义
——HTTP 服务天然带生命周期（起线程、占端口），而"卸载时把端口干净地放掉"正是
effect/disposer 的用武之地。P2 时代的页面服务是手工起的（`create_server()` +
主线程 `serve_forever()`），没有卸载路径；本插件把它变成一对干净的语义：

    setup   → 建 ThreadingHTTPServer + 起步线程 + provide `webui` 服务（含 URL）
    dispose → shutdown + join + server_close（端口释放，无残留）

依赖与边界：

- `inject = ("agent", "sessions")`：缺了装载期就报错（fail loud），而不是
  打开页面才 500；
- **线程边界**：HTTP 服务在自己的线程里跑，但只**调用**服务、不改 ctx 状态；
  ctx 的装载/卸载只在主线程做（内核是同步的，没有锁）；
- **会话串行化**：agent 服务的会话缓存不是线程安全的（多个标签页同时提问会
  踩到它），本插件用一把锁把"跑 turn"串起来——同一时刻只有一个 turn 在执行。

API（静态页面在 `webui/index.html`，纯原生 JS、零依赖）：

    GET  /                          页面
    GET  /api/ledger                插件台账 / 服务 / 工具面（"完全插件化"的可见性）
    GET  /api/sessions              会话列表（turns / last_seq / 最近状态）
    GET  /api/sessions/<id>/events  某会话的事件流 + 投影出的消息（历史由日志投影）
    POST /api/sessions/<id>/send    {text} → NDJSON 流：主循环事件逐条推，末行 __done

已知边界（刻意的）：只绑本机、无鉴权、事件流只推给发起请求的那个客户端
（多客户端广播属于 RPC / 事件流跟随的题目）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from components.session import derive_messages
from kernel import Context
from plugins.services import (
    AGENT_SERVICE,
    APPROVAL_SERVICE,
    SESSIONS_SERVICE,
    TOOLS_SERVICE,
    WEBUI_SERVICE,
)

#: chunked 传输的行分隔符（HTTP 规范硬性要求 \r\n，不能是 \n）。
_CRLF = chr(13).encode("ascii") + chr(10).encode("ascii")

#: 静态页面路径（与本插件同包目录结构：mini_harness/webui/index.html）。
PAGE_PATH = Path(__file__).resolve().parents[1] / "webui" / "index.html"


@dataclass(frozen=True)
class WebUiService:
    """`webui` 服务的形状：只读的连接信息。

    服务的生命周期（线程与端口）归插件的 effect 管——卸载插件即停服；
    这里不给 stop()，避免出现"服务自己关自己"的二义路径。
    """

    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


# ---------------------------------------------------------------------------
# 只读 API 的数据组装（纯函数，便于单测）
# ---------------------------------------------------------------------------


def _endpoint_of(service: Any) -> str | None:
    """服务的"地址"（有就一并交给页面）：webui 有 .url，rpc 有 .address。

    这是"挂载点"的通用发现规则——页面不必认识任何插件：服务自己暴露地址，
    页面就能把它挂成入口（如 token-usage 的用量页面挂在顶栏按钮上）。
    """
    url = getattr(service, "url", None)
    if isinstance(url, str):
        return url
    address = getattr(service, "address", None)
    if isinstance(address, tuple) and len(address) == 2:
        return f"{address[0]}:{address[1]}"
    return None


def _ledger(ctx: Context) -> dict[str, Any]:
    """插件台账 / 服务 / 工具面——把"完全插件化"变成页面上一眼可见的事实。"""
    tools_service = ctx.get(TOOLS_SERVICE)
    return {
        "plugins": [
            {
                "name": info.name,
                "status": info.status,
                "inject": list(info.inject),
                "missing": list(info.missing),
            }
            for info in ctx.plugins
        ],
        "services": ctx.services,
        # 有地址的服务（.url / .address）顺手带给页面：主页面据此挂载入口
        # （token-usage 的用量页面就是这么挂上去的——通用规则，非特例）。
        "endpoints": {
            name: url
            for name in ctx.services
            if (url := _endpoint_of(ctx.get(name))) is not None
        },
        "tools": [spec.name for spec in tools_service.specs()] if tools_service is not None else [],
    }


def _sessions_overview(sessions) -> dict[str, Any]:
    """会话列表：id / turn 数 / 最新 seq / 最近一个 turn 的收尾状态。"""
    items: list[dict[str, Any]] = []
    for session_id in sessions.list_sessions():
        events, _ = sessions.load(session_id)
        turns = sum(1 for event in events if event.type == "turn/start")
        last_end = next((event for event in reversed(events) if event.type == "turn/end"), None)
        items.append(
            {
                "id": session_id,
                "turns": turns,
                "last_seq": events[-1].seq if events else 0,
                "last_status": last_end.data.get("status") if last_end else None,
            }
        )
    return {"sessions": items}


def _session_detail(sessions, session_id: str) -> dict[str, Any]:
    """某会话的事件流 + 由日志投影出的消息（历史由日志派生，不另存一份）。"""
    events, _ = sessions.load(session_id)
    messages = derive_messages(events)
    return {
        "session": session_id,
        "events": [event.to_dict() for event in events],
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tools": [call.name for call in message.tool_calls],
            }
            for message in messages
        ],
    }


# ---------------------------------------------------------------------------
# HTTP 处理器（闭包持有服务与锁；每个请求一个实例）
# ---------------------------------------------------------------------------


def _make_handler(ctx: Context, agent, sessions, lock: threading.Lock, state: dict[str, Any]):
    """构造处理器类：路由 + 流式 turn + 会话串行化。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "p3-mini-harness-webui/0.1"
        #: HTTP/1.1 才能用 chunked 传输——**流式响应的前提**。
        #: 默认（HTTP/1.0）下没有 Content-Length 也没有 chunked，客户端只能
        #: 等连接关闭才认账，于是"逐字到达"变成"一次全到"（实测踩到）。
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # 静音默认访问日志（写 stderr 会刷屏）；需要排查时再打开。
            return

        # ---- 响应工具 ----

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Any) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # ---- 路由 ----

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                self._send_bytes(200, "text/html; charset=utf-8", PAGE_PATH.read_bytes())
            elif route == "/api/ledger":
                self._send_json(200, _ledger(ctx))
            elif route == "/api/sessions":
                self._send_json(200, _sessions_overview(sessions))
            elif route.startswith("/api/sessions/") and route.endswith("/events"):
                session_id = unquote(route[len("/api/sessions/") : -len("/events")])
                self._send_json(200, _session_detail(sessions, session_id))
            elif route == "/api/approvals":
                policy = ctx.get(APPROVAL_SERVICE)
                pending = policy.pending_list() if policy is not None else []
                self._send_json(200, {"pending": pending})
            else:
                self._send_json(404, {"error": f"未知路由：{route}"})

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            if route.startswith("/api/sessions/") and route.endswith("/send"):
                session_id = unquote(route[len("/api/sessions/") : -len("/send")])
                self._stream_turn(session_id)
            elif route.startswith("/api/approvals/"):
                request_id = unquote(route[len("/api/approvals/") :])
                body = self._read_json()
                policy = ctx.get(APPROVAL_SERVICE)
                if policy is None:
                    self._send_json(404, {"error": "当前没有审批策略"})
                elif policy.resolve(request_id, bool(body.get("approved")), str(body.get("note") or "")):
                    self._send_json(200, {"ok": True, "id": request_id,
                                          "approved": bool(body.get("approved"))})
                else:
                    self._send_json(404, {"error": f"没有等待中的审批：{request_id}"})
            else:
                self._send_json(404, {"error": f"未知路由：{route}"})

        # ---- 流式 turn ----

        def _stream_turn(self, session_id: str) -> None:
            body = self._read_json()
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "缺少 text"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            # 逐段推送的三个要点：
            # 1) HTTP/1.1（见 Handler.protocol_version）——HTTP/1.0 不带
            #    Content-Length 时客户端只能等连接关闭才认账，"逐字到达"会退化成
            #    "一次全到"（实测踩到）；
            # 2) 显式 chunked 传输——否则同样要等连接关闭；
            # 3) 每帧写完 flush（见 _write_event），否则攒在缓冲区里一起发。
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")   # 中间层别缓冲（nginx 惯例）
            self.end_headers()
            self._alive = True
            self._chunked = True

            def emit(kind: str, payload: dict[str, Any]) -> None:
                self._write_event(kind, payload)

            # 整个推送过程包在 try/finally 里：**任何**退出路径（正常、异常、
            # 提前 return）都必须写终止块，否则客户端会一直等下一帧。
            try:
                # 会话缓存与剧本 provider 都不是线程安全的：turn 在这里串行执行。
                # 审批通道也在这把锁**之内**挂/摘：保证"问人"永远问当前真正在跑的
                # 那个 turn 的连接（锁外挂会让等待中的 turn 互相抢通道）。
                policy = ctx.get(APPROVAL_SERVICE)
                with lock:
                    handle = None
                    if policy is not None and hasattr(policy, "attach_stream"):
                        handle = policy.attach_stream(emit)
                    opened = agent.open(session_id, on_event=emit)
                    if opened.repaired and session_id not in state["repaired_reported"]:
                        state["repaired_reported"].add(session_id)
                        emit("__repair", {"message": "上次会话被强杀，日志尾部已自动修复"})
                    try:
                        result = agent.ask(text, session_id=session_id)
                    except Exception as exc:  # 兜底：异常也以帧的形式告知前端，不炸服务
                        emit("__error", {"message": f"turn 执行失败：{exc}"})
                        return
                    finally:
                        if handle is not None and hasattr(policy, "detach_stream"):
                            policy.detach_stream(handle)  # 只摘自己挂的那个（身份校验）
                emit(
                    "__done",
                    {
                        "turn": result.turn,
                        "status": result.status,
                        "steps": len(result.steps),
                        "final_text": result.final_text,
                    },
                )
            finally:
                self._close_stream()

        def _close_stream(self) -> None:
            """流式响应收尾：写零长度 chunk 结束分块传输（客户端才停止等待）。"""
            if getattr(self, "_chunked", False):
                try:
                    self.wfile.write(b"0" + _CRLF + _CRLF)
                    self.wfile.flush()
                except (OSError, ValueError):
                    pass
                self._chunked = False
            self._alive = False

        def _write_event(self, kind: str, payload: dict[str, Any]) -> None:
            if not self._alive:
                return
            line = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False, default=str)
            data = line.encode("utf-8") + b"\n"
            try:
                if getattr(self, "_chunked", False):
                    # chunked 分帧：<十六进制长度> CRLF <正文> CRLF
                    self.wfile.write(format(len(data), "X").encode("ascii") + _CRLF + data + _CRLF)
                else:
                    self.wfile.write(data)
                self.wfile.flush()
            except (OSError, ValueError):
                # 客户端走了/连接已收尾：不再写，但让 turn 跑完。
                # OSError 覆盖 WinError 10038/10053/10054 等各平台断线错误；
                # ValueError = 底层文件已关闭（请求处理结束后 wfile 被关）。
                self._alive = False

    return Handler


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class WebUiPlugin:
    """提供 `webui` 服务：本机 HTTP 界面（装载即起，卸载即停）。"""

    name = "webui"
    inject = (AGENT_SERVICE, SESSIONS_SERVICE)

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8766) -> None:
        self._host = host
        self._port = port

    def setup(self, ctx: Context):
        agent = ctx.require(AGENT_SERVICE)
        sessions = ctx.require(SESSIONS_SERVICE)
        lock = threading.Lock()
        state: dict[str, Any] = {"repaired_reported": set()}

        handler = _make_handler(ctx, agent, sessions, lock, state)
        server = ThreadingHTTPServer((self._host, self._port), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="p3-webui", daemon=True)
        thread.start()

        # port=0 时由系统分配空闲端口：真实端口以 server_address 为准。
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        ctx.provide(WEBUI_SERVICE, WebUiService(host=str(bound_host), port=int(bound_port)))

        def dispose() -> None:
            server.shutdown()       # 停接收循环（阻塞至 serve_forever 退出）
            thread.join(timeout=5)  # 等线程收尾
            server.server_close()   # 关监听套接字 → 端口释放

        return dispose


def webui_plugin(*, host: str = "127.0.0.1", port: int = 8766) -> WebUiPlugin:
    """工厂：`port=0` 由系统分配空闲端口（真实地址在 `webui` 服务的 url 里）。"""
    return WebUiPlugin(host=host, port=port)


__all__ = ["WebUiPlugin", "WebUiService", "webui_plugin"]
