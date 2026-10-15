"""`_06` 机制测试：**工具面能被管理**（署名 / 开关 / 台账 / 卸载回卷）。

这一组测的是"工具面的管理语义"本身，与界面无关：

- 登记必须署名（owner）：漏写当场报错，不留空位；
- 停用 = 从可见面撤掉（模型看不到也调不到），登记与实现都还在；
- 卸载工具插件 = 它登记的工具全部回卷，别的插件登记的不受影响（解耦）；
- tool-admin 快照按 owner 分组、动作策略由它算好（界面零知识）；
- 路由与自述随插件生命周期来去（卸载 tools → 端点消失）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.tools import ToolRegistry, define_tool
from kernel import Context
from plugins import (
    TOOLS_ADMIN_SERVICE,
    TOOLS_SERVICE,
    boot_offline,
    memory_fs_plugin,
    tool_calc_plugin,
    tool_fs_plugin,
    tool_search_plugin,
    webui_plugin,
)
from plugins.tools import ToolsPlugin


def _ctx_with_tools(*extra) -> Context:
    """最小装配：内存 fs + 工具底座 + 给定工具插件。"""
    ctx = Context()
    ctx.load_plugins([memory_fs_plugin(), ToolsPlugin(), *extra])
    return ctx


def _boot(tmp_path: Path, **kwargs):
    return boot_offline(
        root=tmp_path / "sessions", workspace=tmp_path / "ws",
        script=None, **kwargs,
    )


# ---------------------------------------------------------------------------
# 署名（owner）：登记必须说清"这是谁的工具"
# ---------------------------------------------------------------------------


def test_registration_requires_an_owner():
    """**漏署名当场报错**——工具台账靠它分组，空署名会让管理面失明。"""
    registry = ToolRegistry()
    definition = define_tool("x", "示例")
    with pytest.raises(ValueError) as excinfo:
        registry.register(definition, lambda: {}, owner="")
    assert "署名" in str(excinfo.value)
    with pytest.raises(ValueError):
        registry.register(definition, lambda: {}, owner="   ")

    registry.register(definition, lambda: {"ok": True}, owner="demo")
    assert registry.owners() == {"demo": ["x"]}


def test_owner_lands_in_the_snapshot_and_groups_tools(tmp_path: Path):
    """台账按 owner 分组：一眼看出每件工具是谁给的。"""
    booted = _boot(tmp_path)
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)
    snapshot = admin.snapshot()

    groups = {g["category"]: g for g in snapshot["groups"]}
    assert {"tools", "tool-calc", "tool-fs", "tool-search", "tool-shell"} <= set(groups)
    assert [p["name"] for p in groups["tool-calc"]["plugins"]] == ["calculate"]
    assert groups["tool-fs"]["label"] == "tool-fs"
    assert "来自插件 tool-fs" in groups["tool-fs"]["hint"]
    # 每件工具都带署名（管理面的第一信息）
    for group in snapshot["groups"]:
        for entry in group["plugins"]:
            assert entry["category"] == group["category"]
    booted.close()


# ---------------------------------------------------------------------------
# 开关：停用只从可见面撤掉
# ---------------------------------------------------------------------------


def test_disabling_hides_the_tool_but_keeps_the_registration(tmp_path: Path):
    """**停用 = 从工具面撤掉**：模型看不到、调不到；登记与实现在册，随时开回来。

    这正是不做成"卸载再重装"的原因：重装等于把插件整个重来一遍（还会牵动
    依赖），而停用应当只影响**可见面**。
    """
    booted = _boot(tmp_path)
    registry = booted.ctx.require(TOOLS_SERVICE)
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)
    assert "calculate" in registry.names
    assert registry.registered("calculate") is not None

    result = admin.disable("calculate")
    assert result["ok"] is True and "已停用" in result["message"]
    # 可见面：没了
    assert "calculate" not in registry.names
    assert "calculate" not in [spec.name for spec in registry.specs()]
    assert registry.resolve("calculate") is None        # 模型调它 → unknown_tool
    # 登记：还在（管理面看得见、能开回来）
    assert registry.registered("calculate") is not None
    assert registry.entries()["calculate"].owner == "tool-calc"
    assert registry.disabled == ["calculate"]

    assert admin.enable("calculate")["ok"] is True
    assert "calculate" in registry.names
    assert registry.disabled == []
    booted.close()


def test_disable_enable_reject_wrong_states_and_unknown_tools(tmp_path: Path):
    booted = _boot(tmp_path)
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)

    assert admin.enable("calculate")["ok"] is False            # 本来就开着
    assert admin.disable("calculate")["ok"] is True
    assert admin.disable("calculate")["ok"] is False           # 本来就停着
    assert admin.enable("calculate")["ok"] is True

    missing = admin.disable("nope")
    assert missing["ok"] is False and "不存在" in missing["message"]
    assert admin.act("calculate", "不存在的动作")["ok"] is False
    booted.close()


def test_unknown_tool_call_is_rejected_after_disable(tmp_path: Path):
    """停用的工具对**循环**来说等于不存在（不是"调用了才发现被拒"）。"""
    from components.tools import ToolPipeline

    booted = _boot(tmp_path)
    registry = booted.ctx.require(TOOLS_SERVICE)
    booted.ctx.require(TOOLS_ADMIN_SERVICE).disable("calculate")
    pipeline = ToolPipeline(registry)
    result = pipeline.run("calculate", {"expression": "1+1"})
    assert result.status == "unknown_tool"
    booted.close()


def test_disabled_state_does_not_survive_unregistration(tmp_path: Path):
    """停用状态跟着登记一起消失（不留孤儿：重装同一插件时工具是**可用**的）。"""
    ctx = _ctx_with_tools(tool_calc_plugin())
    registry = ctx.require(TOOLS_SERVICE)
    registry.set_enabled("calculate", False)
    ctx.unload_plugin("tool-calc")
    assert registry.registered("calculate") is None
    assert registry.disabled == []

    ctx.load_plugin(tool_calc_plugin())
    assert "calculate" in registry.names          # 重装后可用，不是"记得上次停用"


# ---------------------------------------------------------------------------
# 解耦：装卸一个工具插件不影响别的
# ---------------------------------------------------------------------------


def test_tool_plugins_are_independent_of_each_other(tmp_path: Path):
    """**互不依赖**：任卸一个，别的工具插件照常登记、照常可用。"""
    ctx = _ctx_with_tools(tool_calc_plugin(), tool_fs_plugin(), tool_search_plugin())
    registry = ctx.require(TOOLS_SERVICE)

    ctx.unload_plugin("tool-search")
    assert not {"search_text", "find_files"} & set(registry.names)
    assert {"calculate", "read_file"} <= set(registry.names)

    ctx.unload_plugin("tool-calc")
    assert "calculate" not in registry.names
    assert "read_file" in registry.names

    ctx.unload_plugin("tool-fs")
    assert registry.names == ["list_tools", "install_tool"]   # 只剩底座自己的
    assert registry.owners() == {"tools": ["list_tools", "install_tool"]}


def test_unloading_an_owning_plugin_from_the_panel(tmp_path: Path):
    """管理面板的"卸载"落在 owner 插件上：工具跟着回卷，报告里说清连带了什么。"""
    booted = _boot(tmp_path)
    registry = booted.ctx.require(TOOLS_SERVICE)
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)

    result = admin.unload_owning_plugin("tool-fs")
    assert result["ok"] is True
    assert "tool-fs" in result["message"] and "4 件工具" in result["message"]
    assert not {"read_file", "write_file", "list_files", "edit_file"} & set(registry.names)
    assert "calculate" in registry.names

    # 对着**具体工具名**点卸载也行（同一语义：撤掉它所属的插件）
    result = admin.unload_owning_plugin("calculate")
    assert result["ok"] is True and "已卸载插件 tool-calc" in result["message"]
    assert "calculate" not in registry.names

    # 已经不在装配里的 owner：如实回报，不炸
    missing = admin.unload_owning_plugin("tool-fs")
    assert missing["ok"] is False and "不在装配里" in missing["message"]
    booted.close()


def test_tool_admin_snapshot_lists_installable_tool_plugins(tmp_path: Path):
    """白名单里没装的工具插件 → "可安装"格子（装了就不再出现）。"""
    booted = _boot(tmp_path)
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)
    installable = {x["name"] for x in admin.snapshot()["installable"]}
    assert installable == set()          # boot_offline 默认都装了

    booted.ctx.unload_plugin("tool-shell")
    installable = {x["name"] for x in admin.snapshot()["installable"]}
    assert installable == {"tool-shell"}
    assert admin.install("tool-shell")["ok"] is True
    assert admin.snapshot()["installable"] == []
    assert "shell" in booted.ctx.require(TOOLS_SERVICE).names
    booted.close()


def test_tool_admin_install_rejects_outside_the_whitelist(tmp_path: Path):
    booted = _boot(tmp_path)
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)
    result = admin.install("rm-rf")
    assert result["ok"] is False and "白名单" in result["message"]
    # 已经装了的也拒绝（幂等提示，而不是静默重装）
    assert admin.install("tool-calc")["ok"] is False
    booted.close()


# ---------------------------------------------------------------------------
# 模型路径：看工具面 / 装新工具插件
# ---------------------------------------------------------------------------


def test_list_tools_tool_reports_ownership_and_switches(tmp_path: Path):
    booted = _boot(tmp_path)
    registry = booted.ctx.require(TOOLS_SERVICE)
    listing = registry.resolve("list_tools").func()
    by_name = {t["name"]: t for t in listing["tools"]}
    assert by_name["calculate"]["owner"] == "tool-calc"
    assert by_name["write_file"]["needs_approval"] is True
    assert by_name["write_file"]["enabled"] is True

    booted.ctx.require(TOOLS_ADMIN_SERVICE).disable("write_file")
    listing = registry.resolve("list_tools").func()
    assert "write_file" in listing["disabled"]
    booted.close()


def test_install_tool_from_source_registers_new_tool_and_rolls_back(tmp_path: Path):
    """**模型自己写工具**：装进一段源码 → 工具出现在工具面；卸载 → 干净回卷。"""
    from components.tools import AutoApprove

    booted = _boot(tmp_path, approval=AutoApprove())
    fs = booted.ctx.require("fs")
    fs.write_text("plugins/my_tool.py", '''
from components.tools import tool
from plugins.services import TOOLS_SERVICE


@tool("shout")
def shout(text: str, times: int = 2) -> dict:
    """把文本重复几遍并转成大写。"""
    return {"result": text.upper() * times}


class ShoutPlugin:
    name = "tool-shout"
    inject = (TOOLS_SERVICE,)

    def setup(self, ctx):
        registry = ctx.require(TOOLS_SERVICE)
        registry.register(shout.definition, shout.func, owner=self.name)
        return lambda: registry.unregister("shout")
''')
    registry = booted.ctx.require(TOOLS_SERVICE)
    assert "shout" not in registry.names

    result = booted.ctx.require(TOOLS_ADMIN_SERVICE).install_plugin_from_source(
        "plugins/my_tool.py"
    )
    assert result["installed"] == "tool-shout", result
    assert result["tools_added"] == ["shout"]
    assert "shout" in registry.names
    assert registry.entries()["shout"].owner == "tool-shout"
    # 真能调用（参数表从签名派生）
    assert registry.resolve("shout").func(text="hi") == {"result": "HIHI"}
    # 自主安装的插件进了台账，也出现在按 owner 分组的台账里
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)
    assert "tool-shout" in {g["category"] for g in admin.snapshot()["groups"]}
    booted.close()


def test_install_tool_denies_without_approval_policy(tmp_path: Path):
    """**fail-closed**：没有审批策略时安装被拒（引入新代码必须有人的那一下）。"""
    booted = _boot(tmp_path)          # 没装 approval
    fs = booted.ctx.require("fs")
    fs.write_text("plugins/x.py", "def factory():\n    return None\n")
    result = booted.ctx.require(TOOLS_ADMIN_SERVICE).install_plugin_from_source("plugins/x.py")
    assert result["code"] == "INSTALL_DENIED"
    assert "fail-closed" in result["error"]
    booted.close()


def test_install_tool_rejects_bad_path_and_broken_source(tmp_path: Path):
    from components.tools import AutoApprove

    booted = _boot(tmp_path, approval=AutoApprove())
    admin = booted.ctx.require(TOOLS_ADMIN_SERVICE)
    assert admin.install_plugin_from_source("notes.txt")["code"] == "INSTALL_BAD_PATH"
    assert admin.install_plugin_from_source("nope.py")["code"] == "INSTALL_READ_FAILED"

    booted.ctx.require("fs").write_text("plugins/bad.py", "this is not python !!!")
    result = admin.install_plugin_from_source("plugins/bad.py")
    assert result["code"] == "INSTALL_PARSE_FAILED"
    booted.close()


# ---------------------------------------------------------------------------
# 管理页自述与端点（壳零知识）
# ---------------------------------------------------------------------------


def test_tools_surface_declares_admin_page_and_routes(tmp_path: Path):
    """底座声明一个管理页（标签/取数/动作）——壳靠它渲染「工具」标签页。"""
    booted = _boot(tmp_path)
    from components.shell import admins_of, routes_of

    surface = booted.ctx.require("tools.surface")
    admins = admins_of(surface)
    assert [a.key for a in admins] == ["tools"]
    assert admins[0].label == "工具"
    assert admins[0].snapshot == "api/tools"
    assert admins[0].action == "api/tools/{name}/{action}"

    routes = routes_of(surface)
    assert {(r.method, r.pattern) for r in routes} == {
        ("GET", "/api/tools"), ("POST", "/api/tools/<name>/<action>"),
    }
    booted.close()


def test_ledger_publishes_the_tools_admin_tab(tmp_path: Path):
    """页面拿到的 admins 里有两个标签页：插件（order=10）在前，工具（20）在后。"""
    import plugins.webui as webui

    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    ledger = webui._ledger(booted.ctx)
    keys = [(a["key"], a["order"]) for a in ledger["admins"]]
    assert keys == [("plugins", 10), ("tools", 20)]
    tools_tab = next(a for a in ledger["admins"] if a["key"] == "tools")
    assert tools_tab["snapshot"] == "api/tools"
    booted.close()


def test_tool_admin_endpoints_serve_and_disappear_with_the_plugin(tmp_path: Path):
    """端点由壳挂载、随插件生命周期来去（自述是服务的一部分）。"""
    from components.tools import AutoDeny

    booted = _boot(tmp_path, approval=AutoDeny("页面动作不经过审批"))
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require("webui").url

    with urllib.request.urlopen(url + "api/tools", timeout=8) as response:
        snapshot = json.loads(response.read().decode("utf-8"))
    assert {g["category"] for g in snapshot["groups"]} >= {"tool-calc", "tool-fs"}

    # 面板动作：停用一件工具
    request = urllib.request.Request(
        url + "api/tools/calculate/disable", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["ok"] is True and payload["snapshot"]["counts"]["disabled"] == 1
    assert "calculate" not in booted.ctx.require(TOOLS_SERVICE).names

    # 卸载底座 → 端点随之消失
    booted.ctx.unload_plugin("tools")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url + "api/tools", timeout=8)
    assert excinfo.value.code == 404
    booted.close()
