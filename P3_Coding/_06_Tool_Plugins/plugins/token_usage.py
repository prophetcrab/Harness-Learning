"""token-usage 插件 —— 用量账本 + 用量页面（P3 `_03` 的观测面）。

三件事：

1. **订阅记账**：订阅 `llm/response`（每次模型调用后广播，带 `usage`）与
   `turn/end`（turn 收尾，带本次合计），把用量累计进内存账本；
2. **对模型可读**：提供 `usage_stats` 工具——模型可以自己查看"这场对话花了多少",
   这对"预算敏感"的场景有用（它得知道自己还剩多少额度）；
3. **对人可读**：提供一个小 HTTP 服务（默认 `127.0.0.1:8767`），给出
   `/`（用量页面）与 `/api/usage`（JSON，**带 CORS 头**，供主界面跨源读取）。

**与主界面的挂载契约**（`_03` 与 `_02` 界面的对接点）：

    /api/ledger 的 endpoints["token-usage"]  →  http://127.0.0.1:8767/
    主界面据此把"📊 Token 用量"按钮挂到顶栏，iframe 打开 /，数字读 /api/usage

这是"服务自报地址、页面通用发现"的规则（主界面不认识本插件，只认 endpoints）。

**只读 + 无副作用**：账本只在内存（进程重启归零，与"每次启动重新装配"一致）；
页面是纯展示。要长期留存应把用量写进会话日志（`_03` 已留 `usage` 事件通道，
见 SessionRecorder 与 `turn/end` 的 usage 字段）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from components.net import pick_port
from components.shell import PanelSpec
from components.tools import define_tool
from kernel import Context
from plugins.events import LLM_RESPONSE
from plugins.services import TOKEN_USAGE_SERVICE, TOOLS_SERVICE

#: `serve_forever` 的轮询间隔。默认 0.5 秒——**shutdown() 要等它下一次醒来
#: 才返回**，于是每次卸载服务白等最多半秒（实测：0.449s vs 0.007s）。
#: 测试里每个用例都装配/卸载，累积非常可观（229 用例曾因此跑 158 秒，
#: 其中约 100 秒纯粹在等这个）。0.05 秒对这几个轻量只读服务毫无压力。
POLL_INTERVAL = 0.05

@dataclass
class UsageRecord:
    """一次模型调用的用量（账本的一行）。

    `session` 是必须的：**turn 号是每会话独立编号的**，只按 turn 聚合会把
    不同会话的同号 turn 混在一起（实测踩到：两个会话的 turn 1 被并成一条）。
    """

    session: str
    turn: int
    prompt_tokens: int
    completion_tokens: int
    model: str = ""

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "turn": self.turn,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total,
            "model": self.model,
        }


@dataclass
class UsageLedger:
    """内存用量账本：按调用记录 + 分 turn / 全局汇总。"""

    records: list[UsageRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: UsageRecord) -> None:
        with self._lock:
            self.records.append(record)

    def snapshot(self) -> dict[str, Any]:
        """给页面与工具用的汇总视图。"""
        with self._lock:
            records = list(self.records)

        per_turn: dict[tuple[str, int], dict[str, int]] = {}
        prompt = completion = 0
        for record in records:
            prompt += record.prompt_tokens
            completion += record.completion_tokens
            slot = per_turn.setdefault(
                (record.session, record.turn),
                {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
            )
            slot["prompt_tokens"] += record.prompt_tokens
            slot["completion_tokens"] += record.completion_tokens
            slot["calls"] += 1

        turns = [
            {
                "session": session,
                "turn": turn,
                "prompt_tokens": slot["prompt_tokens"],
                "completion_tokens": slot["completion_tokens"],
                "total_tokens": slot["prompt_tokens"] + slot["completion_tokens"],
                "calls": slot["calls"],
            }
            for (session, turn), slot in sorted(per_turn.items())
        ]
        sessions = sorted({record.session for record in records})
        return {
            "calls": len(records),
            "sessions": sessions,
            "turns": turns,
            "totals": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
            "recent": [record.to_dict() for record in records[-20:]],
        }

    def reset(self) -> None:
        with self._lock:
            self.records.clear()


# ---------------------------------------------------------------------------
# 页面与 JSON（独立小服务；挂载点由 webui 的 endpoints 发现）
# ---------------------------------------------------------------------------

PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Token 用量</title>
<style>
  body{margin:0;background:#0e1116;color:#e7eaf0;
    font:13px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  header{padding:12px 16px;border-bottom:1px solid #232936;display:flex;
    align-items:center;gap:12px}
  h1{font-size:15px;margin:0;font-weight:600}
  .muted{color:#8b93a5;font-size:12px}
  .cards{display:flex;gap:12px;padding:16px;flex-wrap:wrap}
  .card{flex:1;min-width:150px;background:#161a22;border:1px solid #232936;
    border-radius:10px;padding:12px 14px}
  .card .k{color:#8b93a5;font-size:11px;text-transform:uppercase;
    letter-spacing:.08em}
  .card .v{font-size:22px;font-weight:650;margin-top:4px;
    font-variant-numeric:tabular-nums}
  table{width:calc(100% - 32px);margin:0 16px 16px;border-collapse:collapse;
    font-size:12.5px}
  th,td{border-bottom:1px solid #1c212b;padding:6px 8px;text-align:right}
  th:first-child,td:first-child{text-align:left}
  th{color:#8b93a5;font-weight:600;font-size:11px;text-transform:uppercase;
    letter-spacing:.06em}
  tr:hover td{background:#12161d}
  .bar{height:6px;background:#6ea8fe;border-radius:4px;display:inline-block;
    vertical-align:middle}
  .barwrap{background:#1c212b;border-radius:4px;height:6px;width:120px;
    display:inline-block;overflow:hidden}
  footer{padding:0 16px 18px;color:#5c6478;font-size:11.5px}
  code{background:#1a2029;border:1px solid #262e3b;border-radius:5px;padding:1px 5px}
</style></head>
<body>
<header><h1>📊 Token 用量</h1><span class="muted" id="status">加载中…</span></header>
<div class="cards" id="cards"></div>
<div id="tablewrap"></div>
<footer>数据来自本进程内存账本 · 接口 <code>/api/usage</code>（CORS 已开）·
  <span id="ts"></span></footer>
<script>
const fmt = (n) => (n ?? 0).toLocaleString("en-US");
async function load() {
  const r = await fetch("/api/usage", { cache: "no-store" });
  const d = await r.json();
  const t = d.totals || {};
  document.getElementById("status").textContent =
    `${d.calls ?? 0} 次调用 · ${(d.turns || []).length} 个 turn`;
  document.getElementById("ts").textContent = new Date().toLocaleTimeString();
  document.getElementById("cards").innerHTML = `
    <div class="card"><div class="k">总 tokens</div><div class="v">${fmt(t.total_tokens)}</div></div>
    <div class="card"><div class="k">输入</div><div class="v">${fmt(t.prompt_tokens)}</div></div>
    <div class="card"><div class="k">输出</div><div class="v">${fmt(t.completion_tokens)}</div></div>
    <div class="card"><div class="k">会话数</div><div class="v">${(d.sessions || []).length}</div></div>`;
  const turns = d.turns || [];
  const max = Math.max(1, ...turns.map(x => x.total_tokens));
  document.getElementById("tablewrap").innerHTML = turns.length ? `
    <table><thead><tr><th>会话 / turn</th><th>输入</th><th>输出</th><th>合计</th>
      <th>调用</th><th>占比</th></tr></thead><tbody>` +
    turns.map(x => `<tr><td>${x.session ?? ""} · turn ${x.turn}</td><td>${fmt(x.prompt_tokens)}</td>
      <td>${fmt(x.completion_tokens)}</td><td>${fmt(x.total_tokens)}</td>
      <td>${x.calls}</td><td><span class="barwrap"><span class="bar"
      style="width:${Math.round(100 * x.total_tokens / max)}%"></span></span></td></tr>`)
      .join("") + `</tbody></table>`
    : `<p class="muted" style="padding:0 16px">还没有用量记录——去主界面对话几轮。</p>`;
}
load();
setInterval(load, 2500);
</script></body></html>
"""


class _UsageHandler(BaseHTTPRequestHandler):
    ledger: UsageLedger  # 由插件在启动时注入（类属性）

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        return

    def _send(self, status: int, content_type: str, body: bytes, *, cors: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            # 跨源读取（主界面从别的端口读这个 JSON）——只读接口，放开 GET 即可
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
        elif route == "/api/usage":
            payload = json.dumps(self.ledger.snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload, cors=True)
        else:
            self._send(404, "application/json; charset=utf-8", b'{"error":"\\u672a\\u77e5\\u8def\\u7531"}')


@dataclass(frozen=True)
class UsageEndpoint:
    """`token-usage` 服务的形态：只读地址 + **自己的面板自述**（见 panel/spec.py）。"""

    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def panels(self) -> tuple[PanelSpec, ...]:
        return (
            PanelSpec(
                service=TOKEN_USAGE_SERVICE,
                label="Token",
                title="Token 用量",
                badge_path="api/usage",
                # 合计 token 在 JSON 的 totals.total_tokens 里——壳只认
                # "点号路径取字段"这一种规则，不写业务逻辑。
                badge_field="totals.total_tokens",
                badge_kind="number",
                badge_fallback="Token 用量",
                order=30,
            ),
        )


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class TokenUsagePlugin:
    """提供 `token-usage` 服务与 `usage_stats` 工具；可选起用量页面（默认起）。

    **声明 `inject = (TOOLS_SERVICE,)`**：本插件要往工具面里注册 `usage_stats`，
    与 self_tools / clock 同一个约定（注册工具的插件都声明这个依赖）。
    不声明的话，tools 插件被换掉时内核不知道要重启本插件，`usage_stats`
    会**永久消失**（实测：换掉 tools 后它就不在工具面里了）。

    **账本挂在实例上，不在 setup 里新建**：声明 inject 之后，"依赖被换掉 →
    本插件停用 → 依赖回来 → 重新激活"时 `setup` 会再跑一遍；而实例还是同一个
    （内核停用只回卷登记，不丢插件对象）。账本放实例上，历史就**不会因为
    换了个插件而被清空**。真正的卸载（`unload_plugin`）才会丢弃实例——
    那时账本归零才是对的（与"卸载即回卷"一致）。

    这是一条值得记的区分：**插件的"激活状态"（它登记了什么、起了什么服务）
    会被回卷，但插件的"身份与它自己积累的数据"不该被回卷。**
    """

    name = "token-usage"
    #: 应用角色（capability）：观测：用量账本 + 页面——关掉只是看不到账，对话照常。
    category = "capability"
    inject = (TOOLS_SERVICE,)

    def __init__(self, *, serve: bool = True, host: str = "127.0.0.1", port: int = 8767) -> None:
        self._serve = serve
        self._host = host
        self._port = port
        #: 账本与"当前会话/turn"都挂在实例上（见类文档：激活状态 ≠ 插件身份）。
        self._ledger = UsageLedger()
        self._current: dict[str, Any] = {"session": "", "turn": 0}

    def setup(self, ctx: Context):
        ledger = self._ledger
        current = self._current

        def on_turn_start(**payload: Any) -> None:
            current["turn"] = int(payload.get("turn", 0))
            current["session"] = str(payload.get("session", "")) or current["session"]

        def on_response(**payload: Any) -> None:
            response = payload.get("response")
            usage = getattr(response, "usage", None) if response is not None else None
            if usage is None:
                return
            request = payload.get("request")
            model = getattr(request, "model", None) or ""
            ledger.add(
                UsageRecord(
                    session=current["session"] or "(unknown)",
                    turn=current["turn"],
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    model=str(model),
                )
            )

        # 装载期订阅 → 注册即 effect：卸载本插件时自动退订
        ctx.on("turn/start", on_turn_start)
        ctx.on(LLM_RESPONSE, on_response)

        # 模型可读：让它知道自己花了多少（预算敏感场景）
        registry = ctx.require(TOOLS_SERVICE)   # inject 已保证就绪
        registry.register(
            define_tool(
                "usage_stats",
                "查看本会话的 token 用量：总用量、按 turn 的明细、最近几次调用。"
                "只读，无需审批。",
            ),
            ledger.snapshot,
            owner=self.name,   # `_06`：登记要署名（工具台账按 owner 分组）
        )
        disposers: list[Any] = [lambda: registry.unregister("usage_stats")]

        if self._serve:
            handler = type("Handler", (_UsageHandler,), {"ledger": ledger})
            # 端口被别的 harness 占着就换系统分配（见 components/net.py：
            # Windows 上第二个 bind 同一个端口**不报错**，两个进程都以为
            # "我的账本在 8767"，页面徽标就会读到另一个进程的数字）
            port = pick_port(self._host, self._port)
            server = ThreadingHTTPServer((self._host, port), handler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=lambda: server.serve_forever(poll_interval=POLL_INTERVAL),
                name="token-usage", daemon=True,
            )
            thread.start()
            bound_host, bound_port = server.server_address[0], server.server_address[1]
            ctx.provide(
                TOKEN_USAGE_SERVICE,
                UsageEndpoint(host=str(bound_host), port=int(bound_port)),
            )

            def stop_server() -> None:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            disposers.append(stop_server)
        else:
            ctx.provide(TOKEN_USAGE_SERVICE, UsageEndpoint(host=self._host, port=0))

        def dispose() -> None:
            for fn in reversed(disposers):  # 后进先出：先撤工具注册，再停服务
                fn()

        return dispose


def token_usage_plugin(
    *, serve: bool = True, host: str = "127.0.0.1", port: int = 8767
) -> TokenUsagePlugin:
    """工厂：`serve=False` 只记账、不起页面（测试常用）；`port=0` 由系统分配。"""
    return TokenUsagePlugin(serve=serve, host=host, port=port)


__all__ = [
    "PAGE_HTML",
    "TokenUsagePlugin",
    "UsageEndpoint",
    "UsageLedger",
    "UsageRecord",
    "token_usage_plugin",
]
