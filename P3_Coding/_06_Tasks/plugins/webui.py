"""webui 插件 —— 浏览器界面：装载即起服务，卸载即停服（界面也是一个插件）。

这个插件回答的是"界面能不能也是插件"：能，而且它比多数能力**更**需要插件语义
——HTTP 服务天然带生命周期（起线程、占端口），而"卸载时把端口干净地放掉"正是
effect/disposer 的用武之地。P2 时代的页面服务是手工起的（`create_server()` +
主线程 `serve_forever()`），没有卸载路径；本插件把它变成一对干净的语义：

    setup   → 建 ThreadingHTTPServer + 起步线程 + provide `webui` 服务（含 URL）
    dispose → shutdown + join + server_close（端口释放，无残留）

**依赖声明：为什么是 optional 而不是 inject**（`_05` 重构）

最初写的是 `inject = ("agent", "sessions")`，看着很自然，但有个要命的后果：
`agent` 依赖 `llm`，模型一旦用自扩展工具装卸 `llm`（或用 `load_plugin` 换模型），
整条链会连锁停用——**界面服务当场停掉、端口关闭、你眼前的页面失联**。
实测：卸载 llm 后 `webui` 从 active 列表里消失、端口不再监听；重装 llm 后
虽然插件回来了，但**端口已经换了一个**，旧标签页再也连不上。

这正是 `optional` 要解决的（见 kernel/plugin.py 的语义表）：声明成可选依赖后，
`webui` 的生命周期只由"装载它/卸载它"决定，**不受其他插件来去的影响**。
代价是必须在**用的时候**现取服务、取不到时给出人话错误，而不是在 setup 里
抓一份存着。这里用 `_require_or_none` 落实这条纪律（每次现取，拿不到就给 503 人话）。

依赖与边界：

- `optional = ("agent", "sessions")`：缺了照常起服务（页面会显示"服务未就绪"，
  而不是让整个界面消失）；`webui` 插件本身永不因依赖变动被重启；
- **线程边界**：HTTP 服务在自己的线程里跑，但只**调用**服务、不改 ctx 状态；
  ctx 的装载/卸载只在主线程做（内核是同步的，没有锁）；
- **会话串行化**：agent 服务的会话缓存不是线程安全的（多个标签页同时提问会
  踩到它），本插件用一把锁把"跑 turn"串起来——同一时刻只有一个 turn 在执行。

API（静态页面在 `webui/`：index.html + app.css + app.js，纯原生 JS、零依赖）：

    GET  /                          页面（index.html）
    GET  /app.css  /app.js          静态资源（白名单，见 STATIC_FILES）
    GET  /api/ledger                插件台账 / 服务 / 工具面（"完全插件化"的可见性）
    GET  /api/sessions              会话列表（turns / last_seq / 最近状态）
    GET  /api/sessions/<id>/events  某会话的事件流 + 投影出的消息（历史由日志投影）
    POST /api/sessions/<id>/send    {text} → NDJSON 流：主循环事件逐条推，末行 __done
    POST /api/sessions/<id>/compact 手动压缩（`/compact`）→ NDJSON 流：
                                    成功末行 __compact_done / 无可压 __compact_noop
    GET  /api/sessions/<id>/todo     当前任务清单（`_06`；未写过时 todos=null）
    POST /api/sessions/<id>/todo     {line} 执行一条 `/todo ...` 子命令（不花模型的钱）

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
from components.todo import run_todo_command
from kernel import Context
from plugins.services import (
    AGENT_SERVICE,
    APPROVAL_SERVICE,
    SESSIONS_SERVICE,
    TODOS_SERVICE,
    TOOLS_SERVICE,
    WEBUI_SERVICE,
)

#: chunked 传输的行分隔符（HTTP 规范硬性要求 \r\n，不能是 \n）。
_CRLF = chr(13).encode("ascii") + chr(10).encode("ascii")

#: 界面静态目录（与本插件同包目录结构：mini_harness/webui/）。
WEB_DIR = Path(__file__).resolve().parents[1] / "webui"

#: 静态资源白名单：文件名 → Content-Type。
#: **刻意是白名单而不是"按路径读文件"**——这是一台对外（虽然只绑本机）的 HTTP
#: 服务，任何形式的路径拼接都可能被 `../` 走出目录。白名单里的名字全是常量，
#: 请求给不出新路径，目录穿越这条攻击面直接不存在。
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


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
                # 可选依赖（`_05`）：页面据此标"降级运行"——缺了不禁用，但要说清楚
                # 哪几样现在是缺的（如换模型途中 agent 短暂不在）。
                "optional": list(info.optional),
                "missing_optionals": list(info.missing_optionals),
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


def _make_handler(ctx: Context, lock: threading.Lock, state: dict[str, Any]):
    """构造处理器类：路由 + 流式 turn + 会话串行化。

    **服务一律现取，不在这里抓一份存着**（`_05` 查出的纪律）：`agent` /
    `sessions` 是可选依赖，它们可能被卸下又装回（换模型、自扩展工具装卸），
    抓存的旧引用会指向一个已经停用的服务。`ctx.get()` 每次问当前值——
    服务不在时返回 None，由各路由给出人话响应。
    """

    def _require_or_none(name: str) -> Any | None:
        return ctx.get(name)

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

        def _send_static(self, name: str) -> None:
            """发一个静态文件（只认 STATIC_FILES 白名单里的名字）。

            404 而不是回 index.html：这些是资源不是路由，缺了就是缺了——
            浏览器控制台里给个明确的 404，比悄悄拿到一份 HTML 好排查。
            """
            content_type = STATIC_FILES.get(name)
            if content_type is None:
                self._send_json(404, {"error": f"未知静态资源：{name}"})
                return
            path = WEB_DIR / name
            if not path.is_file():
                self._send_json(404, {"error": f"静态资源缺失：{name}"})
                return
            self._send_bytes(200, content_type, path.read_bytes())

        def _send_json(self, status: int, payload: Any) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        def _sessions_or_503(self, build) -> None:
            """需要 `sessions` 服务的只读路由：服务不在时给 503 与人话原因。

            这不是异常路径的兜底，而是**可选依赖的正常分支**——界面插件声明
            sessions 为 optional 之后，"服务暂时不在"必须是一种正常的、说得清
            的响应（见模块 docstring 的语义表）。
            """
            service = _require_or_none(SESSIONS_SERVICE)
            if service is None:
                self._send_json(
                    503, {"error": "会话服务未就绪（sessions 插件未激活）"}
                )
                return
            self._send_json(200, build(service))

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
                self._send_static("index.html")
            elif route in ("/app.css", "/app.js"):
                self._send_static(route.lstrip("/"))
            elif route == "/api/ledger":
                self._send_json(200, _ledger(ctx))
            elif route == "/api/sessions":
                self._sessions_or_503(lambda service: _sessions_overview(service))
            elif route.startswith("/api/sessions/") and route.endswith("/events"):
                session_id = unquote(route[len("/api/sessions/") : -len("/events")])
                self._sessions_or_503(
                    lambda service: _session_detail(service, session_id)
                )
            elif route.startswith("/api/sessions/") and route.endswith("/todo"):
                session_id = unquote(route[len("/api/sessions/") : -len("/todo")])
                self._todo_view(session_id)
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
            elif route.startswith("/api/sessions/") and route.endswith("/compact"):
                session_id = unquote(route[len("/api/sessions/") : -len("/compact")])
                self._compact(session_id)
            elif route.startswith("/api/sessions/") and route.endswith("/todo"):
                session_id = unquote(route[len("/api/sessions/") : -len("/todo")])
                self._todo_command(session_id)
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

        # ---- 任务清单（/todo）----
        #
        # 与 `/goal` 同一个套路（`_06`）：两条路由都不跑 turn——`/todo` 是
        # **人直接操作服务**，一分模型的钱都不花。命令的解析与排版在构件层
        # （components/todo/command.py），网页与 REPL 共用同一份。

        def _todo_view(self, session_id: str) -> None:
            todos = _require_or_none(TODOS_SERVICE)
            if todos is None:
                self._send_json(503, {"error": "todo 插件未装载（清单服务不可用）"})
                return
            self._send_json(200, todos.view(session_id))

        def _todo_command(self, session_id: str) -> None:
            todos = _require_or_none(TODOS_SERVICE)
            if todos is None:
                self._send_json(503, {"error": "todo 插件未装载（清单服务不可用）"})
                return
            body = self._read_json()
            line = str(body.get("line") or "")
            result = run_todo_command(todos.store(session_id), line)
            payload: dict[str, Any] = {
                "ok": True,
                "changed": result.changed,
                "text": result.text,
                **todos.view(session_id),
            }
            if result.run_prompt is not None:
                # `/todo run`：把拼好的提示词回给前端，由它走既有的 /send 流去
                # **真跑一个 turn**（那次花模型的钱）。服务端不在这里自己跑，
                # 是为了让"执行"复用 send 那一整条已打磨过的流式通道。
                payload["run_prompt"] = result.run_prompt
            self._send_json(200, payload)

        # ---- 手动压缩（/compact） ----

        def _compact(self, session_id: str) -> None:
            """一次手动压缩：NDJSON 流（复用 send 的帧格式，前端同一套解析）。

            不跑 turn——只折叠历史；但压缩本身要调一次模型（摘要），所以也走
            流式响应：摘要期间的失败/成功都以帧到达，前端不必干等。
            """
            agent = _require_or_none(AGENT_SERVICE)
            if agent is None:
                self._send_json(503, {"error": "agent 服务未就绪（agent 插件未激活）"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._alive = True
            self._chunked = True

            def emit(kind: str, payload: dict[str, Any]) -> None:
                self._write_event(kind, payload)

            try:
                with lock:                      # 与 turn 互斥：不能和运行中的对话并写历史
                    agent.open(session_id, on_event=emit)   # 打开（或恢复）会话，接上事件流
                    try:
                        outcome = agent.compact(session_id)
                    except Exception as exc:    # 失败要说给人听（手动动作不静默）
                        emit("__error", {"message": f"压缩失败：{exc}"})
                        return
                if outcome is None:
                    emit("__compact_noop", {"message": "没有可压缩的历史（只有当前一轮）"})
                else:
                    emit(
                        "__compact_done",
                        {
                            "folded": outcome.folded,
                            "before_tokens": outcome.before_tokens,
                            "after_tokens": outcome.after_tokens,
                            "summary_tokens": outcome.summary_tokens,
                        },
                    )
            finally:
                self._close_stream()

        # ---- 流式 turn ----

        def _stream_turn(self, session_id: str) -> None:
            agent = _require_or_none(AGENT_SERVICE)
            if agent is None:
                self._send_json(503, {"error": "agent 服务未就绪（agent 插件未激活）"})
                return
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
    #: 可选依赖（不是 inject！）：界面要在"换模型 / 装卸插件"时**一直开着**，
    #: 而 agent 依赖 llm——声明成必需的话，模型一装卸 llm 就会连锁停用整个
    #: 界面服务、端口关闭（实测踩到）。见模块 docstring 的完整说明。
    optional = (AGENT_SERVICE, SESSIONS_SERVICE)

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8766) -> None:
        self._host = host
        self._port = port

    def setup(self, ctx: Context):
        lock = threading.Lock()
        state: dict[str, Any] = {"repaired_reported": set()}

        # 依赖**不在这里取**：agent / sessions 是可选依赖，可能被卸下再装回，
        # 抓一份存着就会指向停用的旧服务（见 _make_handler 的说明）。
        handler = _make_handler(ctx, lock, state)
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
