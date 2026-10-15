"""`_05` 收尾：插件管理（分类 + 一键安装/卸载/停用/启用）。

三层各测一段，边界清楚：

    kernel           category 声明、disable/enable 的语义（含级联与重激活陷阱）
    plugins/catalog  共享白名单（模型与界面消费同一份）
    plugin_admin     分类台账、动作策略（base 不可卸）、四个动作、端点

**本文件不测界面**：壳只做"渲染组 + 点按钮 POST 回去"，那是薄的一层，
浏览器实测覆盖（见 README 的实测记录）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kernel import (
    PLUGIN_CATEGORIES,
    Context,
    plugin_category,
)
from kernel.context import DEFAULT_CATEGORY
from plugins import (
    PLUGIN_ADMIN_SERVICE,
    WEBUI_SERVICE,
    boot_offline,
    plugin_admin_plugin,
    webui_plugin,
)
from plugins.catalog import DEFAULT_CATALOG_NAMES, default_catalog
from plugins.plugin_admin import ACTION_LABELS, CATEGORY_LABELS

# ---------------------------------------------------------------------------
# 内核：category 声明
# ---------------------------------------------------------------------------


def test_category_values_and_default():
    assert PLUGIN_CATEGORIES == ("base", "capability", "other")

    class Bare:
        def setup(self, ctx): ...

    assert plugin_category(Bare()) == DEFAULT_CATEGORY == "other"
    assert plugin_category(type("X", (), {"category": "base", "setup": lambda s, c: None})()) == "base"


def test_illegal_category_fails_loud():
    """写错一个字母必须报错——静默降级会让插件悄悄失去它该有的行为。"""

    class Typo:
        category = "capabilty"        # 少了个 i

        def setup(self, ctx): ...

    with pytest.raises(ValueError, match="category 不合法"):
        plugin_category(Typo())
    with pytest.raises(ValueError, match="category 不合法"):
        Context().load_plugin(Typo())


def test_category_lands_in_the_ledger():
    class Based:
        name = "based"
        category = "base"

        def setup(self, ctx): ...

    ctx = Context()
    ctx.load_plugin(Based())
    info = ctx.plugins[0]
    assert info.category == "base"
    assert info.status == "active"


# ---------------------------------------------------------------------------
# 内核：disable / enable
# ---------------------------------------------------------------------------


def _pair_plugins(log: list[str]):
    """一个 provider + 一个依赖它的 consumer（用来测级联）。"""

    class Thing:
        name = "thing"
        category = "capability"

        def setup(self, ctx):
            log.append("thing-setup")
            ctx.provide("thing", "THING")
            return lambda: log.append("thing-dispose")

    class Consumer:
        name = "consumer"
        inject = ("thing",)

        def setup(self, ctx):
            log.append("consumer-setup")
            ctx.require("thing")
            return lambda: log.append("consumer-dispose")

    return Thing(), Consumer()


def test_disable_rolls_back_but_keeps_registration():
    """停用 = 回卷 effect + 保留注册；启用在**同一对象**上重跑 setup。"""
    log: list[str] = []
    thing, consumer = _pair_plugins(log)
    ctx = Context()
    ctx.load_plugin(thing)
    ctx.load_plugin(consumer)

    affected = ctx.disable_plugin("thing")
    assert affected == ["thing", "consumer"]          # 级联：依赖者一起停
    assert ctx.active_plugins == []
    assert ctx.disabled_plugins == ["thing"]
    assert "thing" not in ctx.services                # 服务已撤销
    # 依赖者退回 pending（不是 disabled——它只是等依赖）
    assert [p.status for p in ctx.plugins if p.name == "consumer"] == ["pending"]
    assert log == ["thing-setup", "consumer-setup", "consumer-dispose", "thing-dispose"]

    ctx.enable_plugin("thing")
    assert sorted(ctx.active_plugins) == ["consumer", "thing"]
    assert ctx.disabled_plugins == []
    # setup 又跑了一遍（同一个插件对象）——配置与身份都还在
    assert log[-2:] == ["thing-setup", "consumer-setup"]


def test_disable_does_not_self_reactivate():
    """**回归**：停用不能被 settle 立刻装回去。

    初版实现是"先回卷、事后再把状态改成 disabled"——回卷过程中触发的 settle
    看到它还是 pending、依赖也齐，于是立刻重新激活，`enable` 时报
    `effect 已存在：plugin:thing`（实测踩到）。修法：状态**先设**再回卷。
    """
    log: list[str] = []
    thing, _ = _pair_plugins(log)
    ctx = Context()
    ctx.load_plugin(thing)

    ctx.disable_plugin("thing")
    assert ctx.disabled_plugins == ["thing"]          # 没有偷偷回来
    ctx.enable_plugin("thing")
    assert ctx.active_plugins == ["thing"]            # 现在能正常启用（无 DuplicateSlotError）


def test_disable_and_enable_reject_wrong_states():
    log: list[str] = []
    thing, _ = _pair_plugins(log)
    ctx = Context()
    ctx.load_plugin(thing)

    with pytest.raises(ValueError, match="不需要启用"):
        ctx.enable_plugin("thing")                     # 它是 active
    ctx.disable_plugin("thing")
    with pytest.raises(ValueError, match="已处于停用状态"):
        ctx.disable_plugin("thing")                    # 重复停用
    with pytest.raises(Exception, match="不存在"):
        ctx.disable_plugin("nope")


def test_disabled_plugin_can_still_be_unloaded():
    """停用是"保留注册"，不是"钉住"——随时可以真卸掉。"""
    log: list[str] = []
    thing, _ = _pair_plugins(log)
    ctx = Context()
    ctx.load_plugin(thing)
    ctx.disable_plugin("thing")
    ctx.unload_plugin("thing")
    assert [p.name for p in ctx.plugins] == []


def test_enable_with_missing_dependency_stays_pending():
    """启用不保证激活：依赖缺失时停在 pending，台账里能看到缺什么。"""
    log: list[str] = []
    _, consumer = _pair_plugins(log)
    ctx = Context()
    ctx.load_plugin(consumer)                          # pending（缺 thing）
    ctx.disable_plugin("consumer")                     # 从 pending 直接停用
    assert ctx.disabled_plugins == ["consumer"]
    ctx.enable_plugin("consumer")
    assert ctx.pending_plugins == ["consumer"]         # 诚实地停在这儿
    info = ctx.plugins[0]
    assert info.status == "pending" and info.missing == ("thing",)


# ---------------------------------------------------------------------------
# 共享白名单
# ---------------------------------------------------------------------------


def test_catalog_is_shared_not_duplicated():
    """模型侧与界面侧消费**同一份**白名单（两个消费者各写一份必然漂移）。"""
    from plugins.self_tools import DEFAULT_CATALOG_NAMES as from_self_tools
    from plugins.self_tools import default_catalog as catalog_from_self_tools

    # 按**内容**断言，不按对象身份：本项目的阶段目录各持同名包、conftest 会
    # 驱逐 sys.modules，同一个名字在不同导入时机可能是不同对象（踩过两次）。
    assert tuple(from_self_tools) == DEFAULT_CATALOG_NAMES == ("clock", "webui", "rpc")
    assert list(catalog_from_self_tools()) == list(default_catalog())
    assert DEFAULT_CATALOG_NAMES == ("clock", "webui", "rpc")


def test_shared_catalog_is_loadable(tmp_path: Path):
    # 路径必须用 tmp_path：写相对路径会在仓库里建出真目录（我第一版就是这么
    # 留了个 `__none__/` 空壳——`git status` 看不见空目录，只有人眼能看见）。
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws")
    booted.close()


# ---------------------------------------------------------------------------
# 分类台账与动作策略
# ---------------------------------------------------------------------------


def _boot(tmp_path: Path, **kwargs):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", **kwargs)
    return booted


def test_categories_present_on_all_default_plugins(tmp_path: Path):
    booted = _boot(tmp_path)
    by_name = {p.name: p.category for p in booted.ctx.plugins}
    # 基础插件：没有它们应用不成立
    for name in ("llm", "sessions", "prompt", "tools", "agent"):
        assert by_name.get(name) == "base", f"{name} 应是基础插件"
    # 能力插件：可按需开关
    for name in ("compact", "context-meter", "token-usage", "plugin-admin"):
        assert by_name.get(name) == "capability", f"{name} 应是能力插件"
    # fs / proc 的名字带实现后缀
    assert by_name["fs:MemoryFS"] == "base" and by_name["proc:ScriptedSubprocess"] == "base"
    booted.close()


def test_snapshot_groups_by_category(tmp_path: Path):
    booted = _boot(tmp_path)
    snap = booted.ctx.require(PLUGIN_ADMIN_SERVICE).snapshot()
    labels = [g["label"] for g in snap["groups"]]
    assert labels == [CATEGORY_LABELS[k][0] for k in ("base", "capability", "other")]
    base_names = next(g for g in snap["groups"] if g["category"] == "base")["plugins"]
    assert {p["name"] for p in base_names} >= {"llm", "tools", "agent"}
    assert snap["counts"]["total"] == len(booted.ctx.plugins)
    booted.close()


def test_base_plugins_cannot_be_unloaded_or_disabled(tmp_path: Path):
    """**策略在本模块**：base 不可卸、不可停——壳只显示它下发的动作。"""
    booted = _boot(tmp_path)
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)

    assert admin.unload("llm")["ok"] is False
    assert "不可卸载" in admin.unload("llm")["message"]
    assert admin.disable("tools")["ok"] is False
    assert "最小集合" in admin.disable("tools")["message"]

    # 台账里这些插件的 actions 就是空的（不是壳在判断）
    snap = admin.snapshot()
    base = next(g for g in snap["groups"] if g["category"] == "base")
    assert all(p["actions"] == [] for p in base["plugins"])
    assert all(p["blocked_reason"] for p in base["plugins"])
    booted.close()


def test_capability_plugin_can_be_disabled_and_enabled(tmp_path: Path):
    booted = _boot(tmp_path)
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)
    tools = booted.ctx.require("tools")
    assert "usage_stats" in tools.names

    result = admin.disable("token-usage")
    assert result["ok"] and "已停用" in result["message"]
    # 能力真的撤了：它注册的工具随之消失（effect 回卷）
    assert "usage_stats" not in booted.ctx.require("tools").names

    result = admin.enable("token-usage")
    assert result["ok"]
    assert "usage_stats" in booted.ctx.require("tools").names
    booted.close()


def test_install_from_shared_whitelist(tmp_path: Path):
    booted = _boot(tmp_path)
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)

    assert "clock" not in {p.name for p in booted.ctx.plugins}
    assert admin.install("clock")["ok"] is True
    assert "clock" in {p.name for p in booted.ctx.plugins}
    # 装了之后能从"可安装"里消失
    assert "clock" not in {x["name"] for x in admin.snapshot()["installable"]}

    # 重复安装 / 白名单外 → 人话拒绝，不抛异常
    assert admin.install("clock")["ok"] is False
    assert admin.install("nope")["ok"] is False
    assert "白名单" in admin.install("nope")["message"]

    assert admin.unload("clock")["ok"] is True
    booted.close()


def test_installable_list_excludes_loaded(tmp_path: Path):
    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))       # webui 是白名单里的
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)
    installable = {x["name"] for x in admin.snapshot()["installable"]}
    assert "webui" not in installable
    assert "rpc" in installable
    booted.close()


def test_unknown_action_is_rejected(tmp_path: Path):
    booted = _boot(tmp_path)
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)
    result = admin.act("clock", "explode")
    assert result["ok"] is False and "未知动作" in result["message"]
    booted.close()


def test_action_labels_cover_all_actions():
    assert set(ACTION_LABELS) == {"install", "unload", "disable", "enable"}


def test_admin_can_disable_itself(tmp_path: Path):
    """插件管理把自己关掉是允许的（它就是能力插件）——只是之后没法用界面管了。"""
    booted = _boot(tmp_path)
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)
    result = admin.disable("plugin-admin")
    assert result["ok"] is True
    assert PLUGIN_ADMIN_SERVICE not in booted.ctx.services
    booted.close()


# ---------------------------------------------------------------------------
# 端点（路由自述交给壳）
# ---------------------------------------------------------------------------


def _boot_with_shell(tmp_path: Path):
    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    return booted


def _read(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


def _get_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=8) as response:
        return response.status, response.read().decode("utf-8")


def test_admin_routes_served_by_the_shell(tmp_path: Path):
    booted = _boot_with_shell(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url

    snap = _read(url + "api/plugins")
    assert [g["category"] for g in snap["groups"]] == ["base", "capability", "other"]
    assert snap["actions"]["install"] == "安装"

    request = urllib.request.Request(
        url + "api/plugins/clock/install", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    result = json.loads(urllib.request.urlopen(request, timeout=10).read())
    assert result["ok"] is True and "clock" in result["message"]
    # 动作的返回**带最新快照**——界面点一下就能就地重渲染，不用再请求一次
    assert any(p["name"] == "clock"
               for g in result["snapshot"]["groups"] for p in g["plugins"])
    booted.close()


def test_admin_route_blocks_base_plugin(tmp_path: Path):
    booted = _boot_with_shell(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    request = urllib.request.Request(
        url + "api/plugins/agent/unload", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    result = json.loads(urllib.request.urlopen(request, timeout=10).read())
    assert result["ok"] is False
    assert "不可卸载" in result["message"]
    # 人话拒绝是 200 + ok:false（不是 4xx/5xx）——界面照常显示这句话
    booted.close()


def test_admin_routes_disappear_with_the_plugin(tmp_path: Path):
    """端点随插件生命周期来去（自述是服务的一部分）。"""
    booted = _boot_with_shell(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    _read(url + "api/plugins")                         # 先确认在
    booted.ctx.unload_plugin("plugin-admin")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url + "api/plugins", timeout=8)
    assert excinfo.value.code == 404
    booted.close()


def test_shell_degrades_to_readonly_without_admin(tmp_path: Path):
    """没装 plugin-admin：壳仍能列出插件（只读），不空白也不报错。"""
    booted = _boot_with_shell(tmp_path)
    booted.ctx.unload_plugin("plugin-admin")
    url = booted.ctx.require(WEBUI_SERVICE).url

    # 原始台账照常（壳的通用路由，不依赖任何插件）
    ledger = _read(url + "api/ledger")
    assert [p["name"] for p in ledger["plugins"]]
    assert all("category" in p for p in ledger["plugins"])   # 分类在原始台账里也有
    booted.close()


def test_plugin_admin_plugin_needs_no_other_service():
    """插件管理不依赖任何服务（只用到内核的 ctx 能力）——装配极简。"""
    assert plugin_admin_plugin().setup.__name__ == "setup"
    plugin = plugin_admin_plugin()
    assert getattr(plugin, "inject", ()) == ()
    assert plugin.category == "capability"


def test_admin_panel_is_a_tabbed_popup_driven_by_self_description(tmp_path: Path):
    """管理面板是**独立弹出层 + 标签页**；标签页由插件自述生成（`_06`）。

    演进：`_05` 把台账做成独立弹层（但没有标签的概念）；`_06` 让它变成**通用的
    管理弹层**——插件声明 AdminSpec，壳就多一个标签页。于是"再加一个管理面"
    （这次是工具管理）不用改壳的渲染逻辑，只是多/少一条自述。

    壳仍然零知识：只画 groups/plugins/actions 这套通用形状。
    """
    booted = _boot_with_shell(tmp_path)
    url = booted.ctx.require(WEBUI_SERVICE).url
    _, html = _get_text(url)
    _, js = _get_text(url + "app.js")
    _, css = _get_text(url + "app.css")

    # 骨架：入口门卡 + 弹出层容器 + 标签栏
    for ident in ("ledger-entry", "plugin-modal", "pm-body", "pm-search",
                  "pm-close", "pm-toast", "plugin-count", "pm-tabs"):
        assert f'id="{ident}"' in html, f"面板骨架缺 {ident}"
    # 侧栏里那套内联列表的元素已经拆掉（否则就是搬了没搬干净）
    for stale in ('id="plugins"', "plug-group-h", "pa-btn"):
        assert stale not in html, f"index.html 还留着旧内联列表 {stale}"

    # 行为：弹层开合 / Esc / 遮罩 / 筛选 / 切标签 / 卡片动作全部走事件委托
    assert "function openLedger(" in js and "function closeLedger()" in js
    assert 'e.key === "Escape"' in js
    assert 'e.target.id === "plugin-modal"' in js          # 点遮罩关闭
    assert "function renderTabs()" in js                   # 标签栏按自述渲染
    assert "function applyLedgerFilter()" in js
    assert 'closest(".pm-btn")' in js                      # 动作按钮委托
    assert 'closest(".pm-tab")' in js                      # 标签切换委托
    assert "function ledgerCard(" in js and "ledgerGhostCard" in js
    # 壳依然不认识具体管理面：标签、地址、"可安装"全都来自快照/自述
    assert "data.admins" in js and "spec.snapshot" in js
    assert "snapshot.installable" in js

    # 样式：弹出层的定位 + 标签栏 + 卡片网格（`#plugin-panel` 的教训：改 id 别漏 CSS）
    assert "#plugin-modal{" in css and "position:fixed" in css.split("#plugin-modal{")[1][:120]
    assert ".pm-tab{" in css and ".pm-tabs{" in css
    assert ".pm-card{" in css
    assert "grid-template-columns:repeat(auto-fill" in css
    booted.close()


# ---------------------------------------------------------------------------
# 装配方的角色声明（role）——"它是不是地基"是应用的性质
# ---------------------------------------------------------------------------


def test_loader_role_overrides_the_plugins_own_declaration(tmp_path: Path):
    """**装配方可以覆盖插件自述的角色**：同一个 webui，两种应用两种角色。

    背景（用户指出的设计缺口）："它是不是地基"是**应用的性质，不是插件的性质**。

        webui 在终端组合里是可选的界面（关掉还能用命令行说话）→ capability
        webui 在"界面即产品"的组合里就是地基（没有它应用不成立）      → base

    原先 `category` 只能由插件自己声明，装配方没有话语权——于是
    `python chat.py --web` 这种"就是那个网页"的应用里，webui 仍被当成可停用的
    能力插件，用户能用页面把承载页面的插件关掉（实测：点下去页面当场失联，
    只能回终端装回来）。现在 `load_plugin(role=...)` / `boot(roles=...)` 说了算。
    """
    from plugins import WEBUI_SERVICE, webui_plugin

    booted = _boot(tmp_path)
    # ① 不传 role：沿用插件自述（capability）
    booted.ctx.load_plugin(webui_plugin(port=0))
    info = next(p for p in booted.ctx.plugins if p.name == "webui")
    assert info.category == "capability", "不传 role 时应沿用插件自述"
    booted.ctx.unload_plugin("webui")

    # ② 传 role="base"：覆盖成地基
    booted.ctx.load_plugin(webui_plugin(port=0), role="base")
    info = next(p for p in booted.ctx.plugins if p.name == "webui")
    assert info.category == "base", "装配方声明应覆盖插件自述"
    # 服务照常提供（角色只影响策略，不影响能力）
    assert booted.ctx.require(WEBUI_SERVICE) is not None
    booted.close()


def test_role_is_per_assembly_not_per_plugin_class(tmp_path: Path):
    """**两次装配可以给出不同的角色**（插件类本身没变）。

    这条防的是"把 role 写到插件类上"这种退化改法：那样两次装配就一样了。
    """
    from plugins import webui_plugin

    seen = []
    for role in (None, "base", "other"):
        booted = _boot(tmp_path)
        booted.ctx.load_plugin(webui_plugin(port=0), role=role)
        seen.append(next(p for p in booted.ctx.plugins if p.name == "webui").category)
        booted.close()
    assert seen == ["capability", "base", "other"], f"两次装配的角色应各自独立，实际 {seen}"
    # 插件类自己的声明没被改坏（默认值仍在）
    assert webui_plugin().category == "capability"


def test_bad_role_fails_loud_at_load_time(tmp_path: Path):
    """非法 role 装载期就报错——写错一个字母会让角色悄悄降级成别的。"""
    from plugins import webui_plugin

    booted = _boot(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        booted.ctx.load_plugin(webui_plugin(port=0), role="basel")
    assert "不合法" in str(excinfo.value) and "base" in str(excinfo.value)
    # fail loud 且不留半装：插件没进台账
    assert "webui" not in [p.name for p in booted.ctx.plugins]
    booted.close()


def test_interface_assembly_promotes_webui_to_base_and_hides_its_buttons(tmp_path: Path):
    """**界面即产品**的装配：webui 成为 base → 台账里不给停用/卸载按钮。

    这是本机制要解掉的那个具体问题：`--web` 启动的应用里，用户拿到的东西就是
    那个网页；让他能一键停掉承载页面的插件（页面当场失联）不合理。
    """
    from plugins import INTERFACE_ROLES, webui_plugin

    booted = _boot(tmp_path)
    # 装配方声明（就是 chat.py --web 走的那条路）
    booted.ctx.load_plugin(webui_plugin(port=0), role=INTERFACE_ROLES["webui"])
    admin = booted.ctx.require(PLUGIN_ADMIN_SERVICE)
    entry = next(
        p for g in admin.snapshot()["groups"] for p in g["plugins"] if p["name"] == "webui"
    )
    assert entry["category"] == "base"
    assert entry["actions"] == [], f"地基插件不该给按钮，实际 {entry['actions']}"
    assert "不可卸载" in entry["blocked_reason"]

    # 策略层同样挡住（界面只是不显示按钮，服务端也得拒绝）
    assert admin.disable("webui")["ok"] is False
    assert admin.unload("webui")["ok"] is False
    booted.close()


def test_web_entrypoint_declares_the_interface_role(tmp_path: Path):
    """**`chat.py --web` 那条路真的声明了角色**（端到端接线检查）。

    上面几条验的是机制；这条验的是"那条路确实用了机制"——否则机制写好了却
    没人调用，用户的问题（能用页面关掉承载页面的插件）照旧存在。
    """
    import chat

    booted = _boot(tmp_path)
    chat.start_webui(booted, port=0, role=chat.INTERFACE_ROLE)
    info = next(p for p in booted.ctx.plugins if p.name == "webui")
    assert info.category == "base", "`--web` 是'界面即产品'的组合，webui 应是地基"
    booted.close()

    # REPL 里的 /web 不传 role（终端组合：界面只是观察窗口）→ 仍是能力插件
    booted2 = _boot(tmp_path / "second")
    chat.start_webui(booted2, port=0)
    info2 = next(p for p in booted2.ctx.plugins if p.name == "webui")
    assert info2.category == "capability", "/web 是终端组合，webui 只是可选能力"
    booted2.close()
