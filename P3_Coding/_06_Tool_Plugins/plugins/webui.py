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
- **turn 跑在后台线程，HTTP 请求只是订阅者**（`_05` 收尾改）：
  原先 turn 在**请求线程**里跑、并用一把全局锁串行化。两个后果：不同会话
  不能并行；客户端一断（刷新页面）就再也看不到这个 turn 的进展。
  现在 `TurnRunner` 在后台跑 turn、把帧广播给"当前订阅者"（可多个、可随时来走），
  于是：**不同会话真正并行**、**刷新页面接回同一个 turn**、**不看着也照跑完**。

API —— **壳只提供"通用"路由，插件专属路由由插件自述**（`_05` 收尾改造）：

    壳自己的（与任何具体插件无关）：
      GET  /                          页面（index.html）
      GET  /app.css  /app.js          静态资源（白名单，见 STATIC_FILES）
      GET  /api/ledger                插件台账 / 服务 / 工具面 / 面板与显示自述
      GET  /api/sessions              会话列表（turns / last_seq / 最近状态 / 在跑？）
      GET  /api/sessions/<id>/events  某会话的事件流 + 投影出的消息
      POST /api/sessions/<id>/send    {text} → 起一个 turn 并订阅：NDJSON 逐帧，
                                      末行 __done；该会话已有在跑的 turn → 409
      GET  /api/sessions/<id>/turn    只订阅（页面刷新后接回正在跑的那个）
      POST /api/sessions/<id>/interrupt  请求中断（协作式；没有在跑的 → 404）

    **插件自述的**（壳不认识它们属于哪个插件，只按声明挂载）：
      GET  /api/approvals             审批插件声明
      POST /api/approvals/<id>        审批插件声明
      POST /api/sessions/<id>/compact 压缩插件声明

    四条自述把"界面认识具体插件"从壳里挪回了插件（见 components/shell/）：
      panels   我有个页面 → 给我一个顶栏按钮
      display  我的事件该怎么画 → 壳按四种通用渲染器之一画
      routes   我有专属端点 → 壳挂载并负责传输（分块 / 分帧 / 收尾）
      admins   我有管理面板 → 管理弹层里多一个标签页（`_06`）

已知边界（刻意的）：只绑本机、无鉴权；中断是协作式的（正在跑的**工具调用**
不会被砍断，要等它返回——见 components/agent/runner.py）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from components.agent.runner import TurnRunner
from components.session import derive_messages
from components.shell import RouteTable, admins_of, display_of, panels_of, routes_of
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


#: `serve_forever` 的轮询间隔。默认 0.5 秒——**shutdown() 要等它下一次醒来
#: 才返回**，于是每次卸载服务白等最多半秒（实测：0.449s vs 0.007s）。
#: 测试里每个用例都装配/卸载，累积非常可观（229 用例曾因此跑 158 秒，
#: 其中约 100 秒纯粹在等这个）。0.05 秒对这几个轻量只读服务毫无压力。
POLL_INTERVAL = 0.05

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
                # 应用角色（base / capability / other）：**只读降级**路径也要用它
                # 分组——没装 plugin-admin 时界面仍按分类显示，只是没有按钮。
                "category": info.category,
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
        # 有地址的服务（.url / .address）顺手带给页面：主页面据此挂载入口。
        "endpoints": {
            name: url
            for name in ctx.services
            if (url := _endpoint_of(ctx.get(name))) is not None
        },
        # **面板自述**（`_05` 补齐）：插件在自己那个服务对象上挂 `panels = (...)`,
        # 页面遍历这个列表挂按钮——于是"加一个面板插件"**不用改壳**。
        "panels": _collect_panels(ctx),
        # **管理页自述**（`_06`）：插件声明"我有一个管理面板"（标签文字 +
        # 取数地址 + 动作地址模板）。壳不认识"插件管理"或"工具管理"，
        # 只按这套通用形状渲染——于是"再加一个管理面"不用改壳。
        "admins": _collect_admins(ctx),
        # **显示自述**（`_05` 收尾）：插件声明"我的事件该怎么画"——
        # 前端只按 renderer 名分派到四种通用渲染器，不认识任何具体事件名。
        "display": _collect_display(ctx),
        "tools": [spec.name for spec in tools_service.specs()] if tools_service is not None else [],
    }


def _collect_display(ctx: Context) -> list[dict[str, Any]]:
    """收集所有服务的显示自述，按 order + kind 排序（顺序稳定）。"""
    collected: list[dict[str, Any]] = []
    for name in ctx.services:
        for spec in display_of(ctx.get(name)):
            collected.append(spec.to_dict())
    return sorted(collected, key=lambda item: (item["order"], item["kind"]))


def _collect_admins(ctx: Context) -> list[dict[str, Any]]:
    """收集所有服务的管理页自述，按 order + key 排序（顺序稳定）。

    **不需要"服务的地址"**：管理页的快照与动作都走插件自己声明的路由，而路由
    由壳挂载在壳自己的服务器上——与页面同源。所以这里只收集声明本身。
    """
    collected: list[dict[str, Any]] = []
    for name in ctx.services:
        for spec in admins_of(ctx.get(name)):
            collected.append(spec.to_dict())
    return sorted(collected, key=lambda item: (item["order"], item["key"]))


def _collect_panels(ctx: Context) -> list[dict[str, Any]]:
    """收集所有服务的面板自述（附上服务地址），按 order + 服务名排序。

    写成显式循环而不是多层推导式：生成器里多个 `if` 与海象的求值顺序很容易
    写出"以为会执行、其实被过滤掉了"的代码（这里踩过一次）。循环一眼看得清。

    只收集**有地址**的服务：面板按钮点开要打开那个插件的页面，没地址就没页面
    可开——宁可不出按钮，也不要给用户一个点了没反应的入口。
    """
    collected: list[dict[str, Any]] = []
    for name in ctx.services:
        service = ctx.get(name)
        endpoint = _endpoint_of(service)
        if endpoint is None:
            continue
        for panel in panels_of(service):
            collected.append({**panel.to_dict(), "url": endpoint})
    return sorted(collected, key=lambda item: (item["order"], item["service"]))


def _sessions_overview(sessions, runner: Any = None) -> dict[str, Any]:
    """会话列表：id / turn 数 / 最新 seq / 最近收尾状态 / **是否正在跑 turn**。

    "正在跑"从执行器现取（`runner.running()`），不是从日志猜的——日志里
    一个 turn 的 `turn/start` 与 `turn/end` 之间就是进行中，但没有 turn/end 的
    会话也可能是被强杀了（那是"崩溃修复"的题目），两者必须区分开。
    """
    running = runner.running() if runner is not None else {}
    items: list[dict[str, Any]] = []
    for session_id in sessions.list_sessions():
        events, _ = sessions.load(session_id)
        turns = sum(1 for event in events if event.type == "turn/start")
        last_end = next((event for event in reversed(events) if event.type == "turn/end"), None)
        run = running.get(session_id)
        items.append(
            {
                "id": session_id,
                "turns": turns,
                "last_seq": events[-1].seq if events else 0,
                "last_status": last_end.data.get("status") if last_end else None,
                # 在跑的会话：页面据此在列表里标"运行中"并允许中断。
                "running": run is not None,
            }
        )
    # 正在跑但还没有任何日志的会话（刚发出去、日志还没落）也要列出来，
    # 否则页面上的"新会话"会在列表里消失一小会儿。
    known = {item["id"] for item in items}
    for session_id, run in running.items():
        if session_id not in known:
            items.append(
                {
                    "id": session_id, "turns": 0, "last_seq": 0,
                    "last_status": None, "running": run.running,
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


def _make_handler(ctx: Context, runner: Any, state: dict[str, Any]):
    """构造处理器类：路由 + 流式订阅 + 中断。

    **服务一律现取，不在这里抓一份存着**（`_05` 查出的纪律）：`agent` /
    `sessions` 是可选依赖，它们可能被卸下又装回（换模型、自扩展工具装卸），
    抓存的旧引用会指向一个已经停用的服务。`ctx.get()` 每次问当前值——
    服务不在时返回 None，由各路由给出人话响应。

    `runner` 是 turn 执行器（后台线程 + 多订阅者），见 components/agent/runner.py。
    它是**进程级**的（不属于某个插件），由插件在 setup 里建好后交给处理器闭包。
    """

    def _require_or_none(name: str) -> Any | None:
        return ctx.get(name)

    class Handler(BaseHTTPRequestHandler):
        server_version = "p3-mini-harness-webui/0.1"
        #: HTTP/1.1 才能用 chunked 传输——**流式响应的前提**。
        #: 默认（HTTP/1.0）下没有 Content-Length 也没有 chunked，客户端只能
        #: 等连接关闭才认账，于是"逐字到达"变成"一次全到"（实测踩到）。
        protocol_version = "HTTP/1.1"

        #: turn 执行器（闭包注入；每次请求现用，不做类型假设）。
        _runner = runner

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
                self._sessions_or_503(lambda service: _sessions_overview(service, self._runner))
            elif route.startswith("/api/sessions/") and route.endswith("/turn"):
                session_id = unquote(route[len("/api/sessions/") : -len("/turn")])
                self._watch_turn(session_id)
            elif route.startswith("/api/sessions/") and route.endswith("/events"):
                session_id = unquote(route[len("/api/sessions/") : -len("/events")])
                self._sessions_or_503(
                    lambda service: _session_detail(service, session_id)
                )
            elif self._plugin_routes().dispatch(
                "GET", route, ctx=ctx, body={}, responder=self
            ):
                return          # 命中插件的自述路由（壳不认识那是哪个插件）
            else:
                self._send_json(404, {"error": f"未知路由：{route}"})

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            if route.startswith("/api/sessions/") and route.endswith("/send"):
                session_id = unquote(route[len("/api/sessions/") : -len("/send")])
                self._start_turn(session_id)
            elif route.startswith("/api/sessions/") and route.endswith("/interrupt"):
                session_id = unquote(route[len("/api/sessions/") : -len("/interrupt")])
                self._interrupt_turn(session_id)
            elif self._plugin_routes().dispatch(
                "POST", route, ctx=ctx, body=self._read_json(), responder=self
            ):
                return          # 命中插件的自述路由
            else:
                self._send_json(404, {"error": f"未知路由：{route}"})

        # ---- 插件路由（自述驱动） ----

        def _plugin_routes(self) -> Any:
            """每次请求现建路由表——插件可能被装卸（自述跟着来去）。

            建表很便宜（就是遍历几十个服务读个属性），但**不能缓存**：
            自扩展工具能运行时装卸插件，缓存会让新插件的端点装不上、
            卸载的插件端点还留着。与"服务一律现取"是同一条纪律。
            """
            # 扫**所有**服务（不按名字猜）——自述可以挂在服务对象上，也可以挂在
            # 专门的 Surface 上；只要长着 routes 就收。与面板/显示同一套发现规则。
            specs: list[Any] = []
            for name in ctx.services:
                specs.extend(routes_of(ctx.get(name)))
            return RouteTable.build(specs)

        # ---- 插件路由的回应方式（RouteResponder 的实现） ----

        def json(self, status: int, payload: Any) -> None:   # noqa: A003
            """`RouteRequest.json` 的落地：回一个 JSON 响应。"""
            self._send_json(status, payload)

        def stream(self) -> Any:
            """`RouteRequest.stream` 的落地：开一个 NDJSON 分块响应。

            分块传输、CRLF 分帧、终止块**全在这里**——插件 handler 只写帧。
            这几处最容易写错（实测踩过三次：HTTP/1.0 退化、漏写终止块、
            双重编码），所以集中一处、只写一遍。
            """
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            # 同上：流式响应一律写完即关（被中途放弃的连接不能复用）。
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self._alive = True
            self._chunked = True
            return _FrameWriter(self)

        # ---- 流式 turn（起一个 turn 并订阅它） ----

        def _runner_or_503(self) -> Any | None:
            if self._runner is None:
                self._send_json(503, {"error": "agent 服务未就绪（agent 插件未激活）"})
                return None
            return self._runner

        def _start_turn(self, session_id: str) -> None:
            """POST /send：起一个 turn，然后**订阅**它（与页面刷新后接回同一条路）。

            **顺序很讲究**（踩过）：reserve（忙就 409，此时还没发响应头）→
            发 200 + chunked 头 → 挂订阅者 → launch。倒过来的话，快 turn 会在
            订阅者挂上之前把帧全发完，客户端一帧都收不到。
            """
            runner = self._runner_or_503()
            if runner is None:
                return
            body = self._read_json()
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "缺少 text"})
                return

            run = runner.reserve(session_id, text)
            if run is None:
                # 该会话已有在跑的 turn：**不排队**（排队会让用户以为发出去了）。
                # 前端据此弹"中断 / 等待"，由人来决定，见 app.js 的 busy 分支。
                self._send_json(
                    409,
                    {
                        "error": "这条会话正在跑一个 turn（要发新的，请先中断或等它结束）",
                        "busy": True,
                        "session": session_id,
                    },
                )
                return
            self._stream_turn(run, start=True)

        def _watch_turn(self, session_id: str) -> None:
            """GET /turn：只订阅当前在跑的 turn（页面刷新后接回）。"""
            runner = self._runner_or_503()
            if runner is None:
                return
            run = runner.get(session_id)
            if run is None:
                self._send_json(404, {"error": "该会话当前没有在跑的 turn", "running": False})
                return
            self._stream_turn(run)

        def _interrupt_turn(self, session_id: str) -> None:
            """POST /interrupt：请求中断（协作式）。

            **回 JSON 而不是流**，而且 `ok` 只表示"请求已送达主循环"——
            真正停下要等循环下一次看标记（正在跑的工具调用不砍断）。
            """
            runner = self._runner_or_503()
            if runner is None:
                return
            if not runner.cancel(session_id):
                self._send_json(404, {"error": "该会话当前没有在跑的 turn"})
                return
            self._send_json(200, {"ok": True, "session": session_id})

        def _stream_turn(self, run: Any, *, start: bool = False) -> None:
            """把一条 turn 的帧流给本次请求的调用方（订阅 → 逐帧写 → 收尾）。

            `start=True`：这个 run 是刚 reserve 的、**还没启动**——挂上订阅者后
            才 launch（保证不漏帧，见 _start_turn 的顺序说明）。

            **晚到的订阅者与已完成的 turn**：`subscribe` 返回 False 说明 turn
            已经跑完——此时直接发它的终帧，让客户端立刻收尾，而不是挂在
            "等下一帧"上直到超时（刷新刚好撞上收尾是常见情形）。
            """
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
            # 同上：流式响应一律写完即关（被中途放弃的连接不能复用）。
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self._alive = True
            self._chunked = True

            def sink(kind: str, payload: dict[str, Any]) -> None:
                self._write_event(kind, payload)
                if run.final is not None and (kind, payload) == run.final:
                    # 终帧也是广播出来的（等终帧的订阅者靠它收尾）——记一笔，
                    # 收尾时别重复发一遍（重复的 `__done` 会让前端的"末帧"语义悬空）。
                    self._sent_final = True

            self._sent_final = False

            # 整个推送过程包在 try/finally 里：**任何**退出路径（正常、异常、
            # 提前 return）都必须写终止块，否则客户端会一直等下一帧。
            try:
                if start:
                    # 订阅者已挂上（launch 里 append）→ 现在才真的开跑。
                    self._runner.launch(run, sink=sink)
                    try:
                        run.done.wait()
                    finally:
                        run.unsubscribe(sink)
                elif not run.subscribe(sink):
                    # 已经跑完（或正好在此刻收尾）：终帧一定有了，直接发完收工。
                    final = run.final
                    if final is not None:
                        self._write_event(*final)
                    return
                else:
                    try:
                        # 等终帧。**这不是"跑 turn"**——turn 在后台线程里跑，
                        # 这里只是等它结束（等多久都不影响它继续）。
                        run.done.wait()
                    finally:
                        run.unsubscribe(sink)
                # 自己没收到终帧就补一条：客户端断了、或终帧在 subscribe
                # 之前就发过了。不补的话前端会一直转圈。
                if self._alive and not self._sent_final and run.final is not None:
                    self._write_event(*run.final)
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


class _FrameWriter:
    """流式帧的写入端（`RouteRequest.stream()` 给插件 handler 的就是它）。

    handler 只写帧——**分块、收尾这些杂活在这里**。
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def frame(self, kind: str, payload: dict[str, Any]) -> None:
        self._handler._write_event(kind, payload)

    def close(self) -> None:
        self._handler._close_stream()


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class WebUiPlugin:
    """提供 `webui` 服务：本机 HTTP 界面（装载即起，卸载即停）。"""

    name = "webui"
    #: 应用角色（capability）：界面：没有它 agent 仍可用（终端/RPC 都能说话）。
    category = "capability"
    #: 可选依赖（不是 inject！）：界面要在"换模型 / 装卸插件"时**一直开着**，
    #: 而 agent 依赖 llm——声明成必需的话，模型一装卸 llm 就会连锁停用整个
    #: 界面服务、端口关闭（实测踩到）。见模块 docstring 的完整说明。
    optional = (
        AGENT_SERVICE,        # 跑 turn（缺 → 各路由回 503 人话）
        SESSIONS_SERVICE,     # 会话/台账（缺 → 同上）
        APPROVAL_SERVICE,     # 浏览器审批（缺 → 无待批可展示）
        TOOLS_SERVICE,        # 台账里列工具面（缺 → 那一栏留空）
    )

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8766) -> None:
        self._host = host
        self._port = port

    def setup(self, ctx: Context):
        state: dict[str, Any] = {"repaired_reported": set()}

        # turn 执行器：**turn 跑在后台线程里**，HTTP 请求只是订阅者（见模块 docstring）。
        # 回调从 ctx 现取服务——webui 声明它们是 optional（可能被卸下再装回），
        # 抓一份存着会指向已停用的旧服务。
        def ask_turn(session_id: str, text: str, sink: Any) -> Any:
            """跑一个 turn。`sink` 是这个 turn 的写帧端（执行器给的）。

            **推流器在"轮到我跑"的那一刻才挂**（`on_started`）：这是会话锁之内、
            turn 开始之前。提前挂会收到上一个 turn 的尾巴（实测：第二个 turn 的流
            里先冒出前任的 `step_response / turn_end`，客户端凭空多渲染一条气泡）。

            两处登记（都只在这个 turn 有效，收尾时摘掉）：

            - `agent.watch`：主循环事件（step_request / step_delta / …）；
            - `policy.attach_thread_stream`：审批帧——审批是在**这条 turn 的
              线程**里被调用的，按线程登记就能"各问各的人"。
            """
            agent = ctx.get(AGENT_SERVICE)
            if agent is None:
                raise RuntimeError("agent 服务未就绪（agent 插件未激活）")
            policy = ctx.get(APPROVAL_SERVICE)
            attached = policy is not None and hasattr(policy, "attach_thread_stream")
            unwatch: list[Any] = []          # 挂上之后填，收尾时统一摘

            def on_started() -> None:
                unwatch.append(agent.watch(session_id, lambda kind, payload: sink(kind, payload)))
                if attached:
                    policy.attach_thread_stream(sink, session=session_id)

            try:
                agent.open(session_id)
                return agent.ask(text, session_id=session_id, on_started=on_started)
            finally:
                for stop in unwatch:
                    stop()
                if attached:
                    policy.detach_thread_stream()

        def cancel_turn(session_id: str) -> bool:
            agent = ctx.get(AGENT_SERVICE)
            if agent is None:
                return False
            return bool(agent.cancel(session_id))

        def prepare_turn(session_id: str) -> str | None:
            """开场提示（如日志尾部刚被修复）——只在首次报告，不重复刷屏。"""
            agent = ctx.get(AGENT_SERVICE)
            if agent is None:
                return None
            try:
                opened = agent.open(session_id)
            except Exception:  # noqa: BLE001 —— 开场信息拿不到不该挡住 turn
                return None
            if not opened.repaired or session_id in state["repaired_reported"]:
                return None
            state["repaired_reported"].add(session_id)
            return "上次会话被强杀，日志尾部已自动修复"

        runner = TurnRunner(
            ask=ask_turn, cancel=cancel_turn, prepare=prepare_turn,
            thread_prefix="p3-turn",
        )

        handler = _make_handler(ctx, runner, state)
        server = ThreadingHTTPServer((self._host, self._port), handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=POLL_INTERVAL),
            name="p3-webui", daemon=True,
        )
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
