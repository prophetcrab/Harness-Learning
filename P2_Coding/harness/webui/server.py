"""mini harness 的可视化服务 —— 标准库 http.server，零新增依赖。

为什么用 stdlib 而不是 Flask/FastAPI：本项目的定位是"聚焦 harness 机制、依赖极简"，
可视化只是观察窗口，不值得为它引一套 web 框架。`http.server` + 手写 JSON 路由足够，
且**离线**可用（页面不引 CDN）。

它把 harness 的"事件流"直接变成可视化的东西：

    POST /api/sessions/<id>/send   → NDJSON 流：一边跑 turn，一边把每个事件推给前端
                                     （前端实时画出"日志轨迹"正在长出来）

其余接口（GET 会话/事件、fork、模拟崩溃）都用于"回放/查看"静态日志。

设计上服务端**不持有长活 harness**：每个 send 请求现场 open 一次会话（`MiniHarness.open`
对已存在 id 就是 resume），把 on_event 接到输出流上。这样天然支持 resume，也没有
多请求共享状态的竞态。

用法（在 P2_Coding 目录下）：

    python -m harness.webui.server    # 真实 API（读取项目根 .env 里的 key）
    python -m harness.webui.server --port 8765 --no-open
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from harness.env import build_deepseek_provider
from harness.llm.provider import LLMProvider
from harness.llm.vocabulary import Message
from harness.mini import DEFAULT_SYSTEM_PROMPT, MiniHarness
from harness.runner import fork_session
from harness.session import JsonlStore
from harness.tools import AutoApprove, AutoDeny
from harness.tools.approval import ApprovalPolicy
from harness.tools.workspace import build_workspace_registry

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]  # _05_Mini_Harness/../ = 项目根
DEFAULT_ROOT = HERE.parents[1] / "sessions"
DEFAULT_WORKSPACE = HERE.parents[1] / "demo_workspace"
INDEX_HTML = HERE / "index.html"


# ---------------------------------------------------------------------------
# 配置：把"可注入的依赖"收成一个对象，测试可替换 provider / approval
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    root: Path
    workspace: Path
    provider_factory: Callable[[], LLMProvider]
    provider_label: str
    approval_factory: Callable[[], ApprovalPolicy]
    include_search: bool = False
    max_steps: int = 8
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @property
    def store(self) -> JsonlStore:
        return JsonlStore(self.root)


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def message_to_json(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ],
    }


def event_to_json(event: Any) -> dict[str, Any]:
    return {"seq": event.seq, "ts": event.ts, "type": event.type, "data": event.data}


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniHarnessUI/1.0"

    # ---- 基础工具 ----

    @property
    def config(self) -> ServerConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 默认会往 stderr 打访问日志；静音，避免污染终端
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _write_line(self, obj: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                self._serve_index()
            elif path == "/api/config":
                self._api_config()
            elif path == "/api/tools":
                self._api_tools()
            elif path == "/api/sessions":
                self._api_sessions()
            elif path.startswith("/api/sessions/"):
                self._api_session(path.rsplit("/", 1)[-1])
            else:
                self._send_json(404, {"error": f"未知路径：{path}"})
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        parts = [p for p in path.split("/") if p]  # api/sessions/<id>/<action>
        try:
            if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                session_id, action = parts[2], parts[3]
                if action == "send":
                    return self._api_send(session_id)
                if action == "fork":
                    return self._api_fork(session_id)
                if action == "simulate-crash":
                    return self._api_simulate_crash(session_id)
            self._send_json(404, {"error": f"未知路径：{path}"})
        except BrokenPipeError:
            pass

    # ---- 静态页 ----

    def _serve_index(self) -> None:
        if not INDEX_HTML.is_file():
            self._send_json(500, {"error": f"找不到页面：{INDEX_HTML}"})
            return
        body = INDEX_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- API：只读 ----

    def _api_config(self) -> None:
        self._send_json(
            200,
            {
                "provider": self.config.provider_label,
                "root": str(self.config.root),
                "workspace": str(self.config.workspace),
                "max_steps": self.config.max_steps,
                "include_search": self.config.include_search,
            },
        )

    def _api_tools(self) -> None:
        registry = build_workspace_registry(self.config.workspace, include_search=self.config.include_search)
        self._send_json(
            200,
            {
                "tools": [
                    {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
                    for spec in registry.specs()
                ]
            },
        )

    def _api_sessions(self) -> None:
        store = self.config.store
        items = []
        for sid in store.list_sessions():
            events, _ = store.load(sid)
            items.append(
                {
                    "id": sid,
                    "events": len(events),
                    "turns": sum(1 for e in events if e.type == "turn/end"),
                    "last_type": events[-1].type if events else "",
                }
            )
        self._send_json(200, {"sessions": items})

    def _api_session(self, session_id: str) -> None:
        store = self.config.store
        events, report = store.load(session_id)
        # 只读投影：借用 MiniHarness 不构造 runner，直接用 session 投影
        from harness.session import Session

        session = Session(session_id, store=None, events=events)
        messages = session.derive_messages()
        steps = sum(1 for e in events if e.type == "step/start")
        self._send_json(
            200,
            {
                "id": session_id,
                "events": [event_to_json(e) for e in events],
                "messages": [message_to_json(m) for m in messages],
                "stats": {
                    "events": len(events),
                    "turns": session.last_turn_number,
                    "steps": steps,
                },
                "repair": {
                    "repaired": report.repaired,
                    "dropped_bytes": report.dropped_bytes,
                    "reason": report.reason,
                },
            },
        )

    # ---- API：跑一个 turn（NDJSON 流） ----

    def _api_send(self, session_id: str) -> None:
        body = self._read_json_body()
        text = (body.get("text") or "").strip()
        if not text:
            self._send_json(400, {"error": "缺少 text"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        provider = self.config.provider_factory()
        approval = self.config.approval_factory()

        def emit(kind: str, payload: dict[str, Any]) -> None:
            # on_event 在跑 turn 的同一线程里被调用 → 直接写进响应流，实时可见
            self._write_line({"kind": kind, "payload": payload})

        harness, report = MiniHarness.open(
            session_id,
            provider=provider,
            root=self.config.root,
            workspace=self.config.workspace,
            system_prompt=self.config.system_prompt,
            approval=approval,
            include_search=self.config.include_search,
            max_steps=self.config.max_steps,
            on_event=emit,
        )
        if report.repaired:
            self._write_line(
                {
                    "kind": "__repair",
                    "payload": {"dropped_bytes": report.dropped_bytes, "reason": report.reason},
                }
            )

        try:
            result = harness.send(text)
        except Exception as exc:  # noqa: BLE001 —— 把错误也流给前端展示
            self._write_line({"kind": "__error", "payload": {"message": f"{type(exc).__name__}: {exc}"}})
            return

        self._write_line(
            {
                "kind": "__done",
                "payload": {
                    "turn": result.turn,
                    "status": result.status,
                    "steps": len(result.steps),
                    "final_text": result.final_text,
                },
            }
        )

    # ---- API：fork / 模拟崩溃 ----

    def _api_fork(self, session_id: str) -> None:
        body = self._read_json_body()
        new_id = (body.get("new_id") or "").strip()
        if not new_id:
            self._send_json(400, {"error": "缺少 new_id"})
            return
        upto = body.get("upto")
        upto = int(upto) if upto not in (None, "", 0) else None
        try:
            forked = fork_session(self.config.store, session_id, new_id, upto_seq=upto)
        except FileExistsError as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(200, {"id": new_id, "events": len(forked.events)})

    def _api_simulate_crash(self, session_id: str) -> None:
        """往 jsonl 尾部写半行 JSON，模拟进程被强杀；立刻 load 一次拿修复报告。

        注意：修复报告必须在这里返回。若留给前端"稍后再读"，中间任何一次
        `load()`（例如会话列表接口会遍历所有会话）都会先把它修掉，报告就丢了。
        """
        store = self.config.store
        path = store.path(session_id)
        if not path.is_file():
            self._send_json(404, {"error": f"会话不存在：{session_id}"})
            return
        path.write_bytes(path.read_bytes() + b'{"seq": 999999, "type": "user/mess')
        _, report = store.load(session_id)  # 触发修复并取得报告
        self._send_json(
            200,
            {
                "path": str(path),
                "repaired": report.repaired,
                "dropped_bytes": report.dropped_bytes,
                "reason": report.reason,
            },
        )


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def create_server(config: ServerConfig, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """构造（但不启动）服务器；测试可传入 port=0 取随机端口。"""
    config.root.mkdir(parents=True, exist_ok=True)
    config.workspace.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.config = config  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def build_config(args: argparse.Namespace) -> ServerConfig:
    # 页面直接调用真实 API：provider 每次请求现场构造，读取项目根 .env 里的 key。
    def provider_factory() -> LLMProvider:
        return build_deepseek_provider(PROJECT_ROOT)

    provider_label = "DeepSeekProvider（真实 API）"

    def deny_factory() -> ApprovalPolicy:
        return AutoDeny("服务端以 --deny-writes 启动：禁止一切需审批操作")

    # 页面里无法做 y/n 交互，默认放行（README 已注明）；--deny-writes 时一律拒绝
    approval_factory: Callable[[], ApprovalPolicy] = deny_factory if args.deny_writes else AutoApprove

    return ServerConfig(
        root=Path(args.root),
        workspace=Path(args.workspace),
        provider_factory=provider_factory,
        provider_label=provider_label,
        approval_factory=approval_factory,
        include_search=args.search,
        max_steps=args.max_steps,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server.py", description="mini harness 可视化服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="文件工具工作区")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--search", action="store_true", help="启用 web_search 工具（需网络）")
    parser.add_argument("--deny-writes", action="store_true", help="禁止一切需审批的写操作")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = build_config(args)
    # 提前校验 key（fail loud）：缺 key 时立刻报错，而不是等第一个请求失败。
    try:
        config.provider_factory()
    except SystemExit as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    httpd = create_server(config, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"mini harness 可视化服务已启动：{url}")
    print(f"  provider：{config.provider_label}")
    print(f"  会话日志：{config.root}")
    print(f"  工作区  ：{config.workspace}")
    print("  按 Ctrl+C 停止。")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
