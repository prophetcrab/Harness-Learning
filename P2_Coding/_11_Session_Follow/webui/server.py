"""webui —— P2 当前形态的可视化页面：配置驱动 + 全工具面 + 插件台账可见。

**为什么在阶段顶层而不是改 `harness/webui/`**：P2 的组织约定是 `harness/` 冻结
（M1–M3 基线），新能力放阶段主目录顶层。基线页面（`python -m harness.webui.server`）
继续存在——它是"M1–M3 形态"的历史快照与回归测试对象；本包是"P2 全部机制就位后"
的当前形态：

- **配置驱动**（`_08`/`_09`）：`--profile dev|prod` → `load_profile` 叠层 →
  `boot_tree` 激活；页面头部显示 profile 与每行的来源层（`llm:fake ← dev.yaml`）；
- **全工具面**（`_06`/`_11`）：工具来自 boot 出的 `toolbox` 槽位——calculate +
  read/write/list/edit + search/find + shell，共 8 件（基线页面只有 4 件）；
- **插件台账可见**（`_07`）：`/api/ctx` 暴露 effect 台账与槽位；页面头部显示
  "插件：llm:FakeLLM · fs:MemoryFS · …"——"注册即 effect"在页面上看得见。

实现方式：**子类化基线的 `Handler`**（复用 HTTP 协议细节、会话列表/详情/fork/
模拟崩溃接口与静态页服务），只覆盖三处装配点（`_api_config` / `_api_tools` /
`_api_send`）并新增 `/api/ctx`。send 的装配从 `MiniHarness.open` 换成
`context.open_context_harness`（`_02` 起的新接线：可注入 `tool_registry`、
每 step 渲染、装配单进日志）——与 `chat.py` 走同一条装配路径。

用法（在 _11_Session_Follow 目录下）：

    python webui/server.py                    # dev profile（离线）
    python webui/server.py --profile prod     # 真实 API（读取项目根 .env 的 key）
    python webui/server.py --port 8766 --no-open
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# 路径引导：直接 `python webui/server.py` 时，脚本目录（webui/）在 sys.path 首位，
# 顶层包（harness/prompt/context/...）找不到——把阶段根目录也插进来（幂等）。
_STAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_STAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_STAGE_ROOT))

# 基线的 HTTP 细节（路由/会话接口/静态页）原样复用；装配点由本包覆盖。
from config import ConfigError, ConfigTree, load_profile
from context import collect_runtime_context, open_context_harness
from harness.session import JsonlStore
from harness.tools import AutoApprove, AutoDeny
from harness.tools.approval import ApprovalPolicy
from harness.webui import server as base_web
from kernel import Context
from providers import LLM_CAPABILITY, TOOLBOX_CAPABILITY, boot_tree

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent          # <stage>/webui
STAGE_ROOT = _STAGE_ROOT                        # <stage>
PROFILES_DIR = STAGE_ROOT / "profiles"
DEFAULT_ROOT = STAGE_ROOT / "sessions"
INDEX_HTML = HERE / "index.html"


# ---------------------------------------------------------------------------
# 配置：profile 的最终配置树 + 已激活的 ctx（页面服务共享一份装配）
# ---------------------------------------------------------------------------


@dataclass
class WebConfig:
    """页面服务的配置：一棵配置树 + 一个激活出的 ctx（进程内共享）。

    与基线 `ServerConfig` 的差异：provider / 工具面不再来自 `provider_factory`
    与硬编码的 `build_workspace_registry`，而是来自 **boot_tree 激活出的 ctx
    槽位**——"页面上能用的能力"与配置树逐行对应（装配台账见 `/api/ctx`）。
    """

    profile: str
    tree: ConfigTree
    plug_ctx: Context
    root: Path
    workspace: Path
    approval_factory: Callable[[], ApprovalPolicy] = AutoApprove
    max_steps: int = 8
    plugins: list[str] = field(default_factory=list)  # effect 台账（boot 后填）

    @property
    def store(self) -> JsonlStore:
        return JsonlStore(self.root)

    @property
    def provider_label(self) -> str:
        llm_row = self.tree.get("llm")
        name = llm_row.name if llm_row else "?"
        return f"{name} · profile {self.profile}"

    @property
    def include_search(self) -> bool:
        toolbox_row = self.tree.get("toolbox")
        return bool(toolbox_row and toolbox_row.config.get("include_search"))

    def config_rows(self) -> list[dict[str, Any]]:
        """每行的最终值与来源层（`_09` 的台账在页面上的形态）。"""
        rows = []
        for row in self.tree.rows:
            rows.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "disabled": row.disabled,
                    "source": self.tree.source_of(row.id, "name"),
                    "config_source": self.tree.source_of(row.id, "config"),
                }
            )
        return rows


# ---------------------------------------------------------------------------
# HTTP：子类化基线 Handler，只换装配点
# ---------------------------------------------------------------------------


class Handler(base_web.Handler):
    """基线 Handler 的升级版：装配全部经由 `self.config.plug_ctx`。"""

    @property
    def config(self) -> WebConfig:
        return self.server.config  # type: ignore[attr-defined]

    # ---- 路由：加 /api/ctx ----

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path == "/api/ctx":
            return self._api_ctx()
        return super().do_GET()

    # ---- 静态页：服务本包自己的页面（基线的指向 harness/webui/index.html） ----

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

    # ---- API：只读（覆盖基线，改为读配置树与 ctx） ----

    def _api_config(self) -> None:
        self._send_json(
            200,
            {
                "provider": self.config.provider_label,
                "profile": self.config.profile,
                "plugins": self.config.plugins,
                "rows": self.config.config_rows(),
                "root": str(self.config.root),
                "workspace": str(self.config.workspace),
                "max_steps": self.config.max_steps,
                "include_search": self.config.include_search,
            },
        )

    def _api_ctx(self) -> None:
        """插件台账：effect 名、槽位、工具面计数（`_07` 机制的可视化）。"""
        toolbox = self.config.plug_ctx.require(TOOLBOX_CAPABILITY)
        self._send_json(
            200,
            {
                "profile": self.config.profile,
                "effects": self.config.plug_ctx.effect_names,
                "slots": self.config.plug_ctx.slots,
                "tools": [spec.name for spec in toolbox.specs()],
            },
        )

    def _api_tools(self) -> None:
        toolbox = self.config.plug_ctx.require(TOOLBOX_CAPABILITY)
        self._send_json(
            200,
            {
                "tools": [
                    {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
                    for spec in toolbox.specs()
                ]
            },
        )

    # ---- API：跑一个 turn（NDJSON 流；装配走 open_context_harness） ----

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

        def emit(kind: str, payload: dict[str, Any]) -> None:
            # on_event 在跑 turn 的同一线程里被调用 → 直接写进响应流，实时可见
            self._write_line({"kind": kind, "payload": payload})

        # ★ 新装配路径：provider 与工具面都取自 boot 出的 ctx；
        #   open_context_harness 额外带来：每 step 渲染 + system/message 进日志。
        ctx = open_context_harness(
            session_id,
            provider=self.config.plug_ctx.require(LLM_CAPABILITY),
            root=self.config.root,
            workspace=self.config.workspace,
            context_source=lambda: collect_runtime_context(self.config.workspace),
            approval=self.config.approval_factory(),
            tool_registry=self.config.plug_ctx.require(TOOLBOX_CAPABILITY),
            max_steps=self.config.max_steps,
            on_event=emit,
        )
        if ctx.report.repaired:
            self._write_line(
                {
                    "kind": "__repair",
                    "payload": {"dropped_bytes": ctx.report.dropped_bytes, "reason": ctx.report.reason},
                }
            )

        try:
            result = ctx.harness.send(text)
        except AssertionError as exc:
            # dev profile 的离线剧本是有限队列：用完给一句人话提示（服务不崩）。
            self._write_line(
                {
                    "kind": "__error",
                    "payload": {
                        "message": (
                            f"{exc}（dev profile 的离线剧本已用完：换 --profile prod 走真实 API，"
                            "或重启页面服务重置剧本）"
                        )
                    },
                }
            )
            return
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


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def create_server(
    config: WebConfig, *, host: str = "127.0.0.1", port: int = 8766
) -> ThreadingHTTPServer:
    """构造（但不启动）服务器；测试可传 port=0 取随机端口。"""
    config.root.mkdir(parents=True, exist_ok=True)
    config.workspace.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.config = config  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def build_config(args: argparse.Namespace) -> WebConfig:
    """按 profile 加载配置树并激活 ctx（配置错误 → ConfigError 上抛，fail loud）。"""
    tree = load_profile(args.profile, profiles_dir=PROFILES_DIR, cli_patches=args.patch)
    plug_ctx = boot_tree(tree, stage_root=STAGE_ROOT)

    toolbox_row = tree.get("toolbox")
    raw_workspace = (toolbox_row.config.get("workspace") if toolbox_row else None)
    workspace = Path(str(raw_workspace)) if raw_workspace else STAGE_ROOT / "demo_workspace/ws"
    if not workspace.is_absolute():
        workspace = STAGE_ROOT / workspace

    def deny_factory() -> ApprovalPolicy:
        return AutoDeny("页面以 --deny-writes 启动：禁止一切需审批操作")

    # 页面里无法做 y/n 交互：默认自动放行（与基线页面一致）；--deny-writes 时一律拒绝
    approval_factory: Callable[[], ApprovalPolicy] = deny_factory if args.deny_writes else AutoApprove

    return WebConfig(
        profile=args.profile,
        tree=tree,
        plug_ctx=plug_ctx,
        root=Path(args.root),
        workspace=workspace,
        approval_factory=approval_factory,
        max_steps=args.max_steps,
        plugins=list(plug_ctx.effect_names),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server.py", description="mini harness 可视化服务（P2 形态）")
    parser.add_argument("--profile", default="dev", help="profile 名（dev 离线 / prod 真实 API）")
    parser.add_argument(
        "--patch", action="append", default=[], metavar="FILE",
        help="CLI 补丁层（可多次；与 chat.py 同一套分层机制）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--deny-writes", action="store_true", help="禁止一切需审批的写操作")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = build_config(args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    httpd = create_server(config, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"mini harness 可视化服务（P2 形态）已启动：{url}")
    print(f"  profile ：{config.profile}")
    print(f"  插件台账：{' · '.join(config.plugins)}")
    print(f"  工具面  ：{config.plug_ctx.require(TOOLBOX_CAPABILITY).names}")
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
