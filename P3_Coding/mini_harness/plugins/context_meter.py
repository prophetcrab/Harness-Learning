"""context-meter 插件 —— 上下文容量仪表（P3 `_04` 的机制）。

回答三个问题（对应挂到主界面顶栏的那张卡片）：

    上下文容量   窗口有多大、用了多少（进度条）
    上下文使用   用在哪：消息 / 系统工具 / 系统提示词 / 其他（各自的占比）
    缓存命中     前缀缓存的加权平均命中率（DeepSeek 按"上下文缓存"计费）

**怎么测**（两个信号，一个缩放）：

1. **实测总额**：每次模型调用的响应里带 `usage.prompt_tokens`——这是"模型这次
   真的看到了多少 token"，最可信。所以总额优先用实测值。
2. **构成估算**：没有 tokenizer（不引依赖），按 DeepSeek 官方给的经验比例估算
   字符→token（中文 ≈ 0.6/字，其他 ≈ 0.3/字符），把请求拆成四类分别估。
3. **按实测缩放**：四类估算值按比例缩放到实测总额——**占比来自估算、总量来自
   实测**，两者都各取所长；没有实测（离线 provider / 首个请求前）时全用估算，
   页面上如实标注来源。

**缓存命中**：`usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens`
（`_04` 给 `Usage` 新增的字段）累计后算**加权平均** = Σhit / (Σhit + Σmiss)——
不是各次命中率的算术平均（那样小请求会和大请求等权）。provider 不报缓存数据时
显示"—"（FakeLLM 就不报）。

**挂载**：与 token-usage 同一套契约——服务自报地址（`endpoints["context-meter"]`），
主界面据此把按钮挂到顶栏；页面与 `/api/context`（带 CORS）都在本插件的小服务里。
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from components.context.tokens import (
    DEFAULT_CONTEXT_WINDOW,
    PART_ORDER,
    estimate_tokens,
    measure_parts,
)
from components.llm.provider import LLMRequest
from components.panel import PanelSpec
from kernel import Context
from plugins.events import LLM_REQUEST, LLM_RESPONSE
from plugins.services import CONTEXT_METER_SERVICE

#: `serve_forever` 的轮询间隔。默认 0.5 秒——**shutdown() 要等它下一次醒来
#: 才返回**，于是每次卸载服务白等最多半秒（实测：0.449s vs 0.007s）。
#: 测试里每个用例都装配/卸载，累积非常可观（229 用例曾因此跑 158 秒，
#: 其中约 100 秒纯粹在等这个）。0.05 秒对这几个轻量只读服务毫无压力。
POLL_INTERVAL = 0.05

#: 默认上下文窗口（`_05` 起 = 1M，与压缩共用同一个默认值）。
DEFAULT_CAPACITY = DEFAULT_CONTEXT_WINDOW

#: 四类的颜色（页面用；顺序在构件里定义，这里只给色）。
PART_COLORS = {
    "消息": "#4f8ff7",
    "系统工具": "#6366f1",
    "系统提示词": "#22d3ee",
    "其他": "#64748b",
}


@dataclass
class ContextSnapshot:
    """一次仪表读数（页面的数据源）。"""

    capacity: int
    used: int
    used_source: str                     # "actual" | "estimate"
    percent: float
    parts: list[dict[str, Any]]
    cache: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "used": self.used,
            "used_source": self.used_source,
            "percent": self.percent,
            "parts": self.parts,
            "cache": self.cache,
        }


class ContextMeter:
    """观测 llm/request 与 llm/response，随时给出一次仪表读数。"""

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity 必须 >= 1，收到 {capacity}")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._parts: dict[str, float] = {name: 0.0 for name in PART_ORDER}
        self._estimate_total: float = 0.0
        self._actual_total: int | None = None      # 最近一次响应的实测 prompt_tokens
        self._hit = 0
        self._miss = 0
        self._cache_calls = 0

    # ---- 观测（挂在事件上） ----

    def observe_request(self, request: LLMRequest, **_context: Any) -> None:
        """`llm/request` 瀑布的观察者：记下这次请求的构成（返回 None 表示不改）。"""
        parts = measure_parts(request.messages, request.tools)
        with self._lock:
            self._parts = parts
            self._estimate_total = sum(parts.values())
        return None

    def observe_response(self, **payload: Any) -> None:
        """`llm/response` 的订阅者：取实测总额与缓存命中。"""
        response = payload.get("response")
        usage = getattr(response, "usage", None) if response is not None else None
        if usage is None:
            return
        with self._lock:
            self._actual_total = int(usage.prompt_tokens)
            if usage.cache_reported:
                self._hit += usage.prompt_cache_hit_tokens
                self._miss += usage.prompt_cache_miss_tokens
                self._cache_calls += 1

    # ---- 读数 ----

    def _scaled_parts(self, used: int, estimate_total: float) -> list[dict[str, Any]]:
        """把四类估算缩放到 `used`，保证**四类之和恰好等于 used**（余数给最大项）。"""
        if estimate_total <= 0:
            tokens = {name: 0 for name in PART_ORDER}
        else:
            factor = used / estimate_total
            tokens = {name: math.floor(value * factor) for name, value in self._parts.items()}
            remainder = used - sum(tokens.values())
            if remainder:
                biggest = max(tokens, key=lambda name: tokens[name])
                tokens[biggest] += remainder
        return [
            {
                "name": name,
                "tokens": tokens[name],
                "percent": round(100.0 * tokens[name] / used, 1) if used > 0 else 0.0,
                "color": PART_COLORS[name],
            }
            for name in PART_ORDER
        ]

    def snapshot(self) -> ContextSnapshot:
        with self._lock:
            estimate_total = self._estimate_total
            actual = self._actual_total
            hit, miss, calls = self._hit, self._miss, self._cache_calls

        if actual is not None:
            used, source = actual, "actual"
        else:
            used, source = int(round(estimate_total)), "estimate"

        total_cache = hit + miss
        return ContextSnapshot(
            capacity=self._capacity,
            used=used,
            used_source=source,
            percent=round(100.0 * used / self._capacity, 1) if self._capacity else 0.0,
            parts=self._scaled_parts(used, estimate_total),
            cache={
                "hit_tokens": hit,
                "miss_tokens": miss,
                "hit_rate": (hit / total_cache) if total_cache else None,
                "calls": calls,
            },
        )


# ---------------------------------------------------------------------------
# 页面与 JSON（独立小服务；挂载点由 webui 的 endpoints 发现）
# ---------------------------------------------------------------------------

PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>上下文容量</title>
<style>
  body{margin:0;background:#0e1116;color:#e7eaf0;display:flex;justify-content:center;
    align-items:flex-start;padding:18px;
    font:13px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  .card{width:min(560px,100%);background:#161a22;border:1px solid #232936;
    border-radius:12px;padding:16px 18px}
  .head{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
  .head h1{font-size:15px;margin:0;font-weight:650}
  .head .num{font-size:12.5px;color:#8b93a5;font-variant-numeric:tabular-nums}
  .bar{height:8px;border-radius:99px;background:#232936;overflow:hidden;margin:12px 0 14px}
  .bar i{display:block;height:100%;border-radius:99px;
    background:linear-gradient(90deg,#3b82f6,#60a5fa);transition:width .35s ease}
  .row{display:flex;align-items:center;gap:9px;padding:5px 0}
  .row .dot{width:9px;height:9px;border-radius:99px;flex:none}
  .row .name{flex:1;color:#c9d2e0}
  .row .pct{font-variant-numeric:tabular-nums}
  .row .tk{color:#5c6478;font-size:11.5px;min-width:70px;text-align:right;
    font-variant-numeric:tabular-nums}
  .sep{border-top:1px solid #232936;margin:12px 0 8px}
  .foot{display:flex;justify-content:space-between;font-size:12.5px;color:#8b93a5}
  .foot b{color:#e7eaf0;font-weight:600}
  .note{color:#5c6478;font-size:11.5px;margin-top:10px}
</style></head>
<body><div class="card">
  <div class="head"><h1>上下文容量</h1><span class="num" id="cap">加载中…</span></div>
  <div class="bar"><i id="fill" style="width:0%"></i></div>
  <div id="parts"></div>
  <div class="sep"></div>
  <div class="foot"><span>平均缓存命中率</span><b id="cache">—</b></div>
  <div class="note" id="note"></div>
</div>
<script>
const fmt = (n) => n >= 10000 ? (n / 10000).toFixed(1) + "万"
  : Number(n).toLocaleString("en-US");
async function load() {
  const res = await fetch("/api/context", { cache: "no-store" });
  const d = await res.json();
  const used = d.used ?? 0, cap = d.capacity ?? 0;
  document.getElementById("cap").textContent =
    `${fmt(used)}/${fmt(cap)}（${(d.percent ?? 0).toFixed(1)}%）`;
  document.getElementById("fill").style.width =
    Math.min(100, d.percent ?? 0).toFixed(1) + "%";
  document.getElementById("parts").innerHTML = (d.parts || []).map((p) => `
    <div class="row"><span class="dot" style="background:${p.color || "#64748b"}"></span>
      <span class="name">${p.name}</span>
      <span class="tk">${fmt(p.tokens)}</span>
      <span class="pct">${(p.percent ?? 0).toFixed(1)}%</span></div>`).join("");
  const c = d.cache || {};
  document.getElementById("cache").textContent =
    c.hit_rate == null ? "—" : (c.hit_rate * 100).toFixed(1) + "%";
  const src = d.used_source === "actual" ? "总额来自上次调用的实测"
    : "总额为字符估算（尚无实测）";
  document.getElementById("note").textContent =
    src + "；构成按字符比例估算后缩放。缓存命中说明："
    + (c.calls ? `${c.calls} 次调用有缓存数据` : "本进程暂未获得缓存数据");
}
load();
setInterval(load, 2500);
</script></body></html>
"""


class _MeterHandler(BaseHTTPRequestHandler):
    meter: ContextMeter  # 由插件在启动时注入（类属性）

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        return

    def _send(self, status: int, content_type: str, body: bytes, *, cors: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
        elif route == "/api/context":
            payload = json.dumps(
                self.meter.snapshot().to_dict(), ensure_ascii=False
            ).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload, cors=True)
        else:
            self._send(404, "application/json; charset=utf-8", b'{"error":"unknown route"}')


@dataclass(frozen=True)
class ContextMeterEndpoint:
    """`context-meter` 服务的形态：只读地址 + 容量 + **自己的面板自述**。

    `panels` 是"界面挂载点"的声明（见 components/panel/spec.py）：主界面遍历
    所有服务的这份声明挂按钮，**不认识本插件**——加面板不用改壳。
    """

    host: str
    port: int
    capacity: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def panels(self) -> tuple[PanelSpec, ...]:
        return (
            PanelSpec(
                service=CONTEXT_METER_SERVICE,
                label="📐 上下文",          # 📐
                title="📐 上下文容量",
                badge_path="api/context",
                badge_field="percent",
                badge_kind="percent",
                badge_suffix="%",
                badge_fallback="📐 上下文",
                order=20,
            ),
        )


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class ContextMeterPlugin:
    """提供 `context-meter` 服务与仪表页面（装载即起，卸载即停）。"""

    name = "context-meter"

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        serve: bool = True,
        host: str = "127.0.0.1",
        port: int = 8768,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity 必须 >= 1，收到 {capacity}")
        self._capacity = capacity
        self._serve = serve
        self._host = host
        self._port = port

    def setup(self, ctx: Context):
        meter = ContextMeter(capacity=self._capacity)
        # 观察者订阅（装载期订阅 → 卸载自动退订）：
        #   llm/request  是瀑布 → 处理器返回 None 表示"只看不改"
        #   llm/response 是广播
        ctx.on(LLM_REQUEST, meter.observe_request)
        ctx.on(LLM_RESPONSE, meter.observe_response)

        if not self._serve:
            ctx.provide(
                CONTEXT_METER_SERVICE,
                ContextMeterEndpoint(host=self._host, port=0, capacity=self._capacity),
            )
            return None

        handler = type("Handler", (_MeterHandler,), {"meter": meter})
        server = ThreadingHTTPServer((self._host, self._port), handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=POLL_INTERVAL),
            name="context-meter", daemon=True,
        )
        thread.start()
        bound_host, bound_port = server.server_address[0], server.server_address[1]
        ctx.provide(
            CONTEXT_METER_SERVICE,
            ContextMeterEndpoint(
                host=str(bound_host), port=int(bound_port), capacity=self._capacity
            ),
        )

        def dispose() -> None:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        return dispose


def context_meter_plugin(
    *,
    capacity: int = DEFAULT_CAPACITY,
    serve: bool = True,
    host: str = "127.0.0.1",
    port: int = 8768,
) -> ContextMeterPlugin:
    """工厂：`serve=False` 只记账不起页面（测试用）；`port=0` 由系统分配。"""
    return ContextMeterPlugin(capacity=capacity, serve=serve, host=host, port=port)


__all__ = [
    "DEFAULT_CAPACITY",
    "PAGE_HTML",
    "PART_COLORS",
    "PART_ORDER",
    "ContextMeter",
    "ContextMeterEndpoint",
    "ContextMeterPlugin",
    "ContextSnapshot",
    "context_meter_plugin",
    "estimate_tokens",
    "measure_parts",
]
