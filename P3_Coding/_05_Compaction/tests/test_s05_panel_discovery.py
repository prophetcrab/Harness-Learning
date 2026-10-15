"""`_05` 收尾：界面零知识 —— 面板由插件自述、壳只遍历。

**这组测试钉住的是一条纪律**：加一个面板插件**不用改壳**（`index.html` /
`app.js`），因为壳不认识任何具体插件——它只读插件在服务对象上挂的
`panels` 自述（见 components/shell/panel.py）。

反例（这条纪律修之前的真实状况）：前端写死两处

    { service: "context-meter", ... }   // 📐
    { service: "token-usage",   ... }   // 📊

加第三个面板插件就得改这两个文件——"完全插件化"在界面上是假的。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from components.shell import PanelSpec, panels_of
from plugins import (
    CONTEXT_METER_SERVICE,
    TOKEN_USAGE_SERVICE,
    WEBUI_SERVICE,
    boot_offline,
    webui_plugin,
)


def _boot(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws")
    booted.ctx.load_plugin(webui_plugin(port=0))
    return booted


def _read(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


# ---------------------------------------------------------------------------
# 构件：PanelSpec
# ---------------------------------------------------------------------------


def test_panel_spec_round_trips():
    panel = PanelSpec(
        service="demo", label="🔧 演示", title="演示面板",
        badge_path="api/x", badge_field="a.b", badge_kind="percent",
        badge_suffix="%", order=5,
    )
    data = panel.to_dict()
    assert data["service"] == "demo" and data["badge_field"] == "a.b"
    assert data["badge_kind"] == "percent" and data["order"] == 5
    # 降级文字缺省时用 label（壳不必额外兜底）
    bare = PanelSpec(service="s", label="L", title="T")
    assert bare.to_dict()["badge_fallback"] == "L"


def test_panels_of_reads_none_single_or_sequence():
    """`panels` 属性的三种形态：没有 / 单个 / 序列——内核只按名字闻，不做类型假设。"""

    class Nothing:
        pass

    class Single:
        panels = PanelSpec(service="a", label="A", title="A")

    class Many:
        panels = (
            PanelSpec(service="b", label="B", title="B"),
            PanelSpec(service="c", label="C", title="C"),
        )

    assert panels_of(Nothing()) == []
    assert [p.service for p in panels_of(Single())] == ["a"]
    assert [p.service for p in panels_of(Many())] == ["b", "c"]


def test_panels_of_ignores_junk():
    """属性挂错了（不是 PanelSpec）不炸——壳不该因为一个插件的笔误整页崩。"""

    class Junk:
        panels = ("我不是 PanelSpec", 42)

    assert panels_of(Junk()) == []


# ---------------------------------------------------------------------------
# 两个内置插件自己声明面板
# ---------------------------------------------------------------------------


def test_builtin_plugins_declare_their_own_panels(tmp_path: Path):
    booted = _boot(tmp_path)
    meter = panels_of(booted.ctx.require(CONTEXT_METER_SERVICE))
    usage = panels_of(booted.ctx.require(TOKEN_USAGE_SERVICE))
    assert [p.service for p in meter] == [CONTEXT_METER_SERVICE]
    assert [p.service for p in usage] == [TOKEN_USAGE_SERVICE]
    # 徽标读的是各自的 JSON 接口
    assert meter[0].badge_path == "api/context" and meter[0].badge_field == "percent"
    assert usage[0].badge_path == "api/usage"
    assert usage[0].badge_field == "totals.total_tokens"
    booted.close()


# ---------------------------------------------------------------------------
# 台账把自述交给页面（连同服务地址）
# ---------------------------------------------------------------------------


def test_ledger_publishes_panel_declarations_with_urls(tmp_path: Path):
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    panels = _read(url + "api/ledger")["panels"]
    services = {p["service"] for p in panels}
    assert services == {CONTEXT_METER_SERVICE, TOKEN_USAGE_SERVICE}
    for panel in panels:
        assert panel["url"].startswith("http://127.0.0.1:")   # 地址一并给出
        assert panel["label"] and panel["title"]
    # 排序稳定（order 再按名字）——多次读取顺序一致
    order1 = [p["service"] for p in _read(url + "api/ledger")["panels"]]
    order2 = [p["service"] for p in _read(url + "api/ledger")["panels"]]
    assert order1 == order2
    booted.close()


def test_panel_without_endpoint_is_not_published(tmp_path: Path):
    """自述挂在**没有地址**的服务上时不会出现在面板列表里。

    理由：面板按钮点开要打开那个插件的页面——没有地址就没有页面可开。
    宁可不出按钮，也不要给用户一个点了没反应的入口。
    """
    from components.shell import PanelSpec as Spec

    class Addressless:
        """有自述、没地址——不该被发布。"""
        panels = (Spec(service="ghost", label="👻", title="幽灵面板"),)

    booted = _boot(tmp_path)
    booted.ctx.provide("ghost", Addressless())
    panels = _read(booted.ctx.require(WEBUI_SERVICE).url + "api/ledger")["panels"]
    assert "ghost" not in {p["service"] for p in panels}
    booted.close()


# ---------------------------------------------------------------------------
# 壳里没有插件名（零知识）
# ---------------------------------------------------------------------------


def test_shell_does_not_hardcode_any_plugin(tmp_path: Path):
    """**核心断言**：壳（index.html + app.js）里不该出现任何具体插件的名字。

    加面板插件不用改壳——这条测试就是它的守门人。
    """
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    with urllib.request.urlopen(url, timeout=8) as response:
        html = response.read().decode("utf-8")
    with urllib.request.urlopen(url + "app.js", timeout=8) as response:
        js = response.read().decode("utf-8")

    for banned in ("context-meter", "token-usage"):
        assert banned not in html, f"index.html 里不该写死插件名 {banned}"
        assert banned not in js, f"app.js 里不该写死插件名 {banned}"
    # 旧的两套写死元素也该没了
    for stale in ("ctx-btn", "token-btn", "ctx-panel", "token-panel", "mountPanel"):
        assert stale not in html, f"index.html 里残留了旧写法 {stale}"
        assert stale not in js, f"app.js 里残留了旧写法 {stale}"
    # 通用挂载点都在
    assert 'id="plugin-panel"' in html and 'id="panel-btns"' in html
    assert "panelBar.panels = data.panels" in js       # 遍历自述
    booted.close()


def test_third_panel_plugin_needs_no_shell_change(tmp_path: Path):
    """**端到端证明零知识**：现造一个带面板的插件装进去 → 面板自动出现在列表里。

    关键在"壳一个字节都没改"——这个插件是测试临时定义的对象，壳（index.html /
    app.js）完全不认识它。它出现在 ledger 的 panels 里，就证明"加面板插件
    不用改壳"这件事成立。
    """
    from components.shell import PanelSpec as Spec
    from kernel import Context

    class DemoPanelPlugin:
        name = "demo-panel"

        def setup(self, ctx: Context):
            class Endpoint:
                host = "127.0.0.1"
                port = 9999

                @property
                def url(self) -> str:
                    return f"http://{self.host}:{self.port}/"

                @property
                def panels(self):
                    return (Spec(service="demo-panel", label="🛠 演示", title="演示面板",
                                 badge_path="api/demo", badge_field="n", order=1),)

            ctx.provide("demo-panel", Endpoint())

    booted = _boot(tmp_path)
    before = {p["service"] for p in _read(booted.ctx.require(WEBUI_SERVICE).url + "api/ledger")["panels"]}
    booted.ctx.load_plugin(DemoPanelPlugin())
    after = _read(booted.ctx.require(WEBUI_SERVICE).url + "api/ledger")["panels"]

    assert "demo-panel" in {p["service"] for p in after}, "新插件的面板没被自动发现"
    assert {p["service"] for p in after} - before == {"demo-panel"}
    # order=1 → 排在最前（顺序由插件自述决定，壳不写死）
    assert after[0]["service"] == "demo-panel"
    # 卸载后按钮自然消失（effect 语义：面板是服务的一部分）
    booted.ctx.unload_plugin("demo-panel")
    gone = {p["service"] for p in _read(booted.ctx.require(WEBUI_SERVICE).url + "api/ledger")["panels"]}
    assert "demo-panel" not in gone
    booted.close()


def test_panel_container_has_matching_css(tmp_path: Path):
    """**通用面板容器必须有对应的 CSS** —— 这条是踩坑后补的。

    回归背景：改造时把 `index.html` 的 `#ctx-panel` / `#token-panel` 换成了
    单个 `#plugin-panel`，`app.js` 也跟着改了，**但 `app.css` 里的选择器没改**。
    结果是：面板元素确实"打开"了（`hidden=false`、iframe 也装了 src），
    但样式一条都不匹配 → `position` 不是 `fixed`、没有全屏遮罩 → **屏幕上
    完全看不见**。测试全绿（测试只查元素在不在），只有真在浏览器里点才露馅。

    教训：改元素 id 时，**要同时检查 HTML / JS / CSS 三处**。这条测试盯住
    "容器有样式且样式能定位它"，就是那个坑的守门人。
    """
    booted = _boot(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    with urllib.request.urlopen(url, timeout=8) as response:
        html = response.read().decode("utf-8")
    with urllib.request.urlopen(url + "app.css", timeout=8) as response:
        css = response.read().decode("utf-8")

    assert 'id="plugin-panel"' in html
    # 通用容器的样式必须在（定位/遮罩/层级）
    assert "#plugin-panel{" in css, "面板容器的样式丢了——它会打开但看不见"
    assert "position:fixed" in css.split("#plugin-panel{")[1][:120]
    assert "z-index:60" in css
    assert "#pp-frame{" in css, "iframe 的样式丢了"
    # 旧的两套写死选择器不许残留（它们指向已不存在的元素）
    for stale in ("#ctx-panel", "#token-panel", "#ctx-frame", "#token-frame",
                  "#ctx-url", "#token-url", "#ctx-open", "#token-open",
                  "#ctx-close", "#token-close", "#ctx-btn", "#token-btn"):
        assert stale not in css, f"CSS 里残留了旧选择器 {stale}（元素已改名）"
        assert stale not in html, f"HTML 里残留了旧元素 {stale}"
    booted.close()
