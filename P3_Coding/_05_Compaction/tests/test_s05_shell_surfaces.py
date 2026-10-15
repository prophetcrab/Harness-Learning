"""`_05` 收尾：壳的两条扩展点 —— 显示自述 与 路由自述。

**为什么要有这两条**：原先壳里写死了**具体插件**的东西——压缩在 `app.js` 里
是 `case "compact_applied"`、在后端是 `POST /api/sessions/<id>/compact`，
审批同理。于是"加一个需要端点或需要特殊渲染的插件"**必须改壳**。

两条自述把这些从壳里挪回插件：

    display  我的事件该怎么画（四种通用渲染器之一）
    routes   我的专属端点（壳负责挂载与传输）

本文件钉住的核心不变量：**壳里不许出现任何具体插件的名字**。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.llm import text_reply
from components.shell import (
    RENDERERS,
    DisplaySpec,
    PanelSpec,
    RouteRequest,
    RouteSpec,
    RouteTable,
    Surface,
    display_of,
    match_path,
    routes_of,
)
from plugins import (
    COMPACTER_SERVICE,
    WEBUI_SERVICE,
    boot_offline,
    webui_plugin,
)


def _boot(tmp_path: Path, *, self_tools: bool = True, script: list | None = None):
    """默认带 self_tools（它会装上 web-approval，于是审批自述也在场）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=script if script is not None else [text_reply("好")],
        self_tools=self_tools,
    )
    booted.ctx.load_plugin(webui_plugin(port=0))
    return booted


def _read(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


# ---------------------------------------------------------------------------
# DisplaySpec：形状与校验
# ---------------------------------------------------------------------------


def test_display_spec_rejects_unknown_renderer():
    """渲染器是**固定四种**——壳不该长成模板引擎，特殊呈现归插件自己的页面。"""
    assert set(RENDERERS) == {"fold", "note", "warn", "status"}
    DisplaySpec("x", "fold")                      # 合法
    with pytest.raises(ValueError, match="未知渲染器"):
        DisplaySpec("x", "chart")                 # 想画图表？那不是壳的事
    with pytest.raises(ValueError, match="kind"):
        DisplaySpec("")


def test_display_spec_round_trips():
    spec = DisplaySpec("some/event", "fold", "折叠 {n} 条", "summary", order=5, terminal=True)
    data = spec.to_dict()
    assert data == {
        "kind": "some/event", "renderer": "fold", "title": "折叠 {n} 条",
        "detail_field": "summary", "order": 5, "terminal": True,
    }


# ---------------------------------------------------------------------------
# 发现：三种自述都能读出来
# ---------------------------------------------------------------------------


def test_surface_carries_all_three_kinds():
    surface = Surface(
        panels=(PanelSpec("s", "L", "T"),),
        display=(DisplaySpec("e", "note"),),
        routes=(RouteSpec("GET", "/api/x", lambda req: None),),
    )
    from components.shell import panels_of as _panels_of

    assert [p.service for p in _panels_of(surface)] == ["s"]
    assert [d.kind for d in display_of(surface)] == ["e"]
    assert [r.pattern for r in routes_of(surface)] == ["/api/x"]


def test_discovery_is_duck_typed_not_isinstance():
    """跨"模块副本"也能识别——不然自述会静默消失（踩过两次）。"""

    class Foreign:            # 长得像 DisplaySpec，但不是同一个类对象
        def to_dict(self):
            return {"kind": "x", "renderer": "note"}

    class Holder:              # 一个普通对象——不是 Surface，也不是 DisplaySpec 的同类
        display = Foreign()

    assert [d.to_dict()["kind"] for d in display_of(Holder())] == ["x"]


def test_discovery_ignores_junk():
    class Junk:
        panels = ("nope", 42)
        display = (object(),)
        routes = ("not a route",)

    from components.shell import panels_of as _panels_of

    assert _panels_of(Junk()) == [] and display_of(Junk()) == [] and routes_of(Junk()) == []


# ---------------------------------------------------------------------------
# RouteTable：匹配与撞车
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("/api/approvals", "/api/approvals", {}),
        ("/api/approvals", "/api/other", None),
        ("/api/sessions/<id>/compact", "/api/sessions/s1/compact", {"id": "s1"}),
        ("/api/sessions/<id>/compact", "/api/sessions/s1/send", None),
        ("/api/sessions/<id>/compact", "/api/sessions/a/b/compact", None),   # 单段
        ("/api/x/<id>", "/api/x/a%20b", {"id": "a b"}),                      # URL 解码
    ],
)
def test_match_path(pattern, path, expected):
    assert match_path(pattern, path) == expected


def test_route_table_rejects_collisions():
    """同 method + 同 pattern 撞车 → 装载期报错，不静默让一个顶掉另一个。"""
    with pytest.raises(ValueError, match="路由撞车"):
        RouteTable.build([
            RouteSpec("GET", "/api/x", lambda r: None),
            RouteSpec("GET", "/api/x", lambda r: None, order=1),
        ])
    # 同路径不同 method 不撞
    assert len(RouteTable.build([
        RouteSpec("GET", "/api/x", lambda r: None),
        RouteSpec("POST", "/api/x", lambda r: None),
    ])) == 2


def test_route_spec_rejects_bad_method():
    with pytest.raises(ValueError, match="不支持的 HTTP 方法"):
        RouteSpec("DELETE", "/api/x", lambda r: None)
    with pytest.raises(ValueError, match="必须以 / 开头"):
        RouteSpec("GET", "api/x", lambda r: None)


# ---------------------------------------------------------------------------
# 两个内置插件的自述
# ---------------------------------------------------------------------------


def test_compact_plugin_declares_display_and_route(tmp_path: Path):
    booted = _boot(tmp_path, self_tools=False)
    surface = booted.ctx.get("compact.surface")
    assert surface is not None, "压缩插件应提供自己的界面自述"
    kinds = {d.kind for d in display_of(surface)}
    assert {"compact/checkpoint", "compact_applied", "compact_failed"} <= kinds
    # 收尾帧标了 terminal（前端据此知道"这是返回值，别画"）
    terminal = {d.kind for d in display_of(surface) if d.terminal}
    assert {"__compact_done", "__compact_noop"} == terminal
    routes = routes_of(surface)
    assert [(r.method, r.pattern) for r in routes] == [("POST", "/api/sessions/<id>/compact")]
    booted.close()


def test_approval_plugin_declares_display_and_routes(tmp_path: Path):
    booted = _boot(tmp_path)          # self_tools 会装上 web-approval
    surface = booted.ctx.get("approval.surface")
    assert surface is not None
    assert [d.kind for d in display_of(surface)] == ["__approval"]
    assert [(r.method, r.pattern) for r in routes_of(surface)] == [
        ("GET", "/api/approvals"),
        ("POST", "/api/approvals/<id>"),
    ]
    booted.close()


# ---------------------------------------------------------------------------
# 壳真的挂上了这些路由
# ---------------------------------------------------------------------------


def test_ledger_publishes_display_specs(tmp_path: Path):
    booted = _boot(tmp_path)
    display = _read(booted.ctx.require(WEBUI_SERVICE).url + "api/ledger")["display"]
    kinds = {d["kind"] for d in display}
    assert "compact/checkpoint" in kinds and "__approval" in kinds
    # 排序稳定
    again = _read(booted.ctx.require(WEBUI_SERVICE).url + "api/ledger")["display"]
    assert [d["kind"] for d in display] == [d["kind"] for d in again]
    booted.close()


def test_shell_serves_plugin_declared_routes(tmp_path: Path):
    """插件的自述路由**真的可用**（壳不认识它是什么插件）。"""
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url

    # GET /api/approvals（审批插件声明）
    assert _read(url + "api/approvals") == {"pending": []}

    # POST /api/sessions/<id>/compact（压缩插件声明）
    request = urllib.request.Request(
        url + "api/sessions/s1/compact", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    body = urllib.request.urlopen(request, timeout=15).read().decode()
    kinds = [json.loads(line)["kind"] for line in body.splitlines() if line.strip()]
    assert kinds == ["__compact_noop"]        # 新会话只有一轮，没什么可压

    # POST /api/approvals/<id>（审批插件声明）
    request2 = urllib.request.Request(
        url + "api/approvals/apv-none", data=b'{"approved":true}',
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request2, timeout=8)
    assert excinfo.value.code == 404
    booted.close()


def test_unknown_route_still_404(tmp_path: Path):
    booted = _boot(tmp_path)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            booted.ctx.require(WEBUI_SERVICE).url + "api/nope", timeout=8
        )
    assert excinfo.value.code == 404
    booted.close()


def test_routes_follow_the_plugin_lifecycle(tmp_path: Path):
    """卸载插件 → 它的端点随之消失（自述是服务的一部分，跟着 effect 回卷）。"""
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    assert _read(url + "api/approvals") == {"pending": []}

    booted.ctx.unload_plugin("web-approval")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url + "api/approvals", timeout=8)
    assert excinfo.value.code == 404, "插件卸载后它的端点应随之消失"
    booted.close()


# ---------------------------------------------------------------------------
# 壳零知识（本次收尾的核心不变量）
# ---------------------------------------------------------------------------


def test_shell_has_no_plugin_specific_business_names(tmp_path: Path):
    """**壳里不许出现任何具体插件的业务词汇**——这是"完全插件化"的守门人。

    允许出现的只有"通用协议"的名字（`__done` / `__error` 这类壳与循环之间的
    约定），以及注释里的举例。插件的端点、事件名、帧名一律由自述提供。
    """
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    with urllib.request.urlopen(url + "app.js", timeout=8) as response:
        js = response.read().decode("utf-8")
    with urllib.request.urlopen(url, timeout=8) as response:
        html = response.read().decode("utf-8")

    # 前端不再认这些**插件专属**的名字
    for banned in ("__compact_done", "__compact_noop", "compact_applied",
                   "compact/checkpoint", "compact_failed"):
        assert banned not in js, f"app.js 里还在认插件专属名字 {banned}"
    # 壳内置的通用协议名允许保留（它们是壳与循环之间的约定）
    assert 'TERMINAL_FRAMES = new Set(["__done", "__error"])' in js
    # 渲染靠自述分派
    assert "function renderBySpec(" in js and "state.display = data.display" in js
    # 面板与路由同理
    assert "context-meter" not in html and "token-usage" not in html
    booted.close()


def test_third_party_display_spec_needs_no_shell_change(tmp_path: Path):
    """**端到端证明**：现造一个声明显示规则的插件 → 事件自动被渲染。

    壳（app.js）一个字节没改——它只按 renderer 名分派。
    """
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    before = {d["kind"] for d in _read(url + "api/ledger")["display"]}

    class DemoEventPlugin:
        name = "demo-event"

        def setup(self, ctx):
            ctx.provide(
                "demo.surface",
                Surface(display=(DisplaySpec("demo/happened", "warn", "演示：{n} 次"),)),
            )

    booted.ctx.load_plugin(DemoEventPlugin())
    after = _read(url + "api/ledger")["display"]
    assert "demo/happened" in {d["kind"] for d in after} - before
    spec = next(d for d in after if d["kind"] == "demo/happened")
    assert spec["renderer"] == "warn" and spec["title"] == "演示：{n} 次"

    booted.ctx.unload_plugin("demo-event")
    gone = {d["kind"] for d in _read(url + "api/ledger")["display"]}
    assert "demo/happened" not in gone
    booted.close()


def test_third_party_route_needs_no_shell_change(tmp_path: Path):
    """现造一个带端点的插件 → 端点自动可用（壳没改）。"""
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url

    class DemoRoutePlugin:
        name = "demo-route"

        def setup(self, ctx):
            def handler(request: RouteRequest) -> None:
                request.json({"hello": request.params["who"]})

            ctx.provide(
                "demo-route",
                Surface(routes=(RouteSpec("GET", "/api/hello/<who>", handler),)),
            )

    booted.ctx.load_plugin(DemoRoutePlugin())
    assert _read(url + "api/hello/world") == {"hello": "world"}
    booted.ctx.unload_plugin("demo-route")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url + "api/hello/world", timeout=8)
    booted.close()


def test_compacter_service_still_present(tmp_path: Path):
    """顺手确认压缩服务没在改造中丢（它是 agent 的必需依赖）。"""
    booted = _boot(tmp_path, self_tools=False)
    assert COMPACTER_SERVICE in booted.ctx.services
    booted.close()
