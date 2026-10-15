"""`_01` 自扩展工具测试：白名单、审批、工具面即时变化、依赖退位、留痕。

验收形状（PLAN.md `_01`）：
- 模型列出的台账与 `ctx.plugins` 一致；
- 白名单内装卸生效（工具面/服务即时变化），卸载后依赖者退位；
- 白名单外直接拒绝（结构化错误、不抛异常）；
- 需审批：拒绝路径下不改动任何东西；
- 装卸在事件流里留痕。
"""

from __future__ import annotations

from pathlib import Path

from components.llm import text_reply, tool_call_reply
from components.tools import ApprovalDecision, ScriptedApprover
from plugins import (
    CLOCK_SERVICE,
    PLUGIN_LOADED,
    PLUGIN_UNLOADED,
    SELF_TOOLS_SERVICE,
    TOOLS_SERVICE,
    boot_offline,
)


def _boot(tmp_path: Path, script: list | None = None, **kwargs):
    return boot_offline(
        root=tmp_path / "sessions", workspace=tmp_path / "ws",
        script=script if script is not None else [text_reply("（测试回答）")],
        self_tools=True, **kwargs,
    )


# ---------------------------------------------------------------------------
# 工具面与台账
# ---------------------------------------------------------------------------


def test_self_tools_adds_three_tools_and_service(tmp_path: Path):
    booted = _boot(tmp_path)
    names = booted.ctx.require(TOOLS_SERVICE).names
    assert {"list_plugins", "load_plugin", "unload_plugin"} <= set(names)
    assert SELF_TOOLS_SERVICE in booted.ctx.services
    assert "self-tools" in booted.ctx.active_plugins
    booted.close()


def test_list_plugins_matches_ctx_ledger_and_reports_whitelists(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    listing = service.list_plugins()

    ledger = {i.name: i.status for i in booted.ctx.plugins}
    assert {p["name"]: p["status"] for p in listing["plugins"]} == ledger
    # `_06`：白名单里多了工具插件（同一张共享表：模型与人看到的是同一份）
    assert listing["loadable"] == [
        "clock", "rpc", "tool-calc", "tool-fs", "tool-search", "tool-shell", "webui",
    ]
    assert listing["unloadable"] == ["clock"]
    assert listing["tools"] == booted.ctx.require(TOOLS_SERVICE).names
    # can_unload 只对"已装载"的插件有意义：此刻 `now` 之外没有白名单插件在装
    assert all(p["can_unload"] is False for p in listing["plugins"])

    # 装上 clock（在可卸白名单里）后，台账里它应标 can_unload=True
    service.load("clock")
    after = {p["name"]: p["can_unload"] for p in service.list_plugins()["plugins"]}
    assert after["clock"] is True
    assert sum(after.values()) == 1
    booted.close()


def test_placeholder_clock_is_not_loaded_by_default(tmp_path: Path):
    booted = _boot(tmp_path)
    assert CLOCK_SERVICE not in booted.ctx.services
    assert "now" not in booted.ctx.require(TOOLS_SERVICE).names
    assert not any(i.name == "clock" for i in booted.ctx.plugins)
    booted.close()


# ---------------------------------------------------------------------------
# 装载 / 卸载（白名单内）
# ---------------------------------------------------------------------------


def test_load_clock_registers_service_and_tool(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)

    result = service.load("clock")
    assert result["loaded"] == "clock"
    assert result["status"] == "active"
    assert result["services_added"] == ["clock"]
    assert result["tools_added"] == ["now"]
    assert CLOCK_SERVICE in booted.ctx.services
    assert "now" in booted.ctx.require(TOOLS_SERVICE).names
    booted.close()


def test_loaded_tool_becomes_available_and_executes(tmp_path: Path):
    """装载后的新工具真的能被调用（不只出现在清单里）。"""
    booted = _boot(tmp_path)
    booted.ctx.require(SELF_TOOLS_SERVICE).load("clock")
    registry = booted.ctx.require(TOOLS_SERVICE)
    tool = registry.resolve("now")
    assert tool is not None
    out = tool.func()
    assert "now" in out and "loaded_at" in out
    booted.close()


def test_unload_clock_rolls_back_service_and_tool(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    service.load("clock")

    result = service.unload("clock")
    assert result["unloaded"] == "clock"
    assert result["services_removed"] == ["clock"]
    assert result["tools_removed"] == ["now"]
    assert CLOCK_SERVICE not in booted.ctx.services
    assert "now" not in booted.ctx.require(TOOLS_SERVICE).names
    # 台账里也不再有它
    assert not any(i.name == "clock" for i in booted.ctx.plugins)
    booted.close()


def test_unloading_self_tools_removes_its_own_tools(tmp_path: Path):
    """自扩展插件自己的卸载也要回卷（三件工具随之注销）。"""
    booted = _boot(tmp_path)
    registry = booted.ctx.require(TOOLS_SERVICE)
    booted.ctx.unload_plugin("self-tools")
    names = registry.names
    assert not {"list_plugins", "load_plugin", "unload_plugin"} & set(names)
    assert SELF_TOOLS_SERVICE not in booted.ctx.services
    booted.close()


# ---------------------------------------------------------------------------
# 白名单边界（拒绝路径：结构化错误、不抛异常）
# ---------------------------------------------------------------------------


def test_load_rejects_name_outside_catalog_without_side_effects(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    before = (set(booted.ctx.services), list(booted.ctx.require(TOOLS_SERVICE).names))

    result = service.load("definitely-not-a-plugin")
    assert result["code"] == "PLUGIN_NOT_ALLOWED"
    assert result["loadable"] == [
        "clock", "rpc", "tool-calc", "tool-fs", "tool-search", "tool-shell", "webui",
    ]
    assert (set(booted.ctx.services), list(booted.ctx.require(TOOLS_SERVICE).names)) == before
    booted.close()


def test_load_duplicate_is_structured_error(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    service.load("clock")
    again = service.load("clock")
    assert again["code"] == "PLUGIN_ALREADY_LOADED"
    booted.close()


def test_unload_rejects_name_outside_unloadable_whitelist(tmp_path: Path):
    """卸载 webui 会切断当前会话的传输通道——默认不放开（哪怕它在可装载名单里）。"""
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    result = service.unload("webui")
    assert result["code"] == "PLUGIN_NOT_ALLOWED"
    assert result["unloadable"] == ["clock"]
    booted.close()


def test_unload_unloaded_plugin_is_structured_error(tmp_path: Path):
    booted = _boot(tmp_path)
    result = booted.ctx.require(SELF_TOOLS_SERVICE).unload("clock")
    assert result["code"] == "PLUGIN_NOT_LOADED"
    booted.close()


def test_custom_whitelists_are_honored(tmp_path: Path):
    """白名单可配：自定义 catalog / unloadable 时按新的来。"""
    from plugins import self_tools_plugin as factory

    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[text_reply("ok")], self_tools=False,
    )
    # 自定义：只允许装 clock，且不允许卸任何东西
    from kernel import Context
    from plugins import (
        AgentPlugin,
        PromptPlugin,
        SessionsPlugin,
        ToolsPlugin,
        fake_llm_plugin,
        memory_fs_plugin,
    )
    from plugins.clock import clock_plugin

    ctx = Context()
    ctx.load_plugins([
        fake_llm_plugin([text_reply("ok")]),
        memory_fs_plugin(),
        SessionsPlugin(tmp_path / "s2"),
        PromptPlugin(workspace=tmp_path / "ws2"),
        ToolsPlugin(),
        factory(catalog={"clock": clock_plugin}, unloadable=()),
        AgentPlugin(),
    ])
    service = ctx.require(SELF_TOOLS_SERVICE)
    assert service.loadable == ["clock"]
    assert service.unloadable == []
    service.load("clock")
    assert service.unload("clock")["code"] == "PLUGIN_NOT_ALLOWED"
    booted.close()


# ---------------------------------------------------------------------------
# 审批（与写文件同级；拒绝路径下不改动任何东西）
# ---------------------------------------------------------------------------


def test_load_plugin_requires_approval_and_denial_changes_nothing(tmp_path: Path):
    approver = ScriptedApprover([ApprovalDecision(False, "用户拒绝")])
    booted = _boot(
        tmp_path,
        script=[
            tool_call_reply("load_plugin", {"name": "clock"}),
            text_reply("好的，装载被拒绝了。"),
        ],
        approval=approver,
    )
    result = booted.agent.ask("装一下 clock", session_id="s1")
    approver.assert_all_consumed()
    assert result.final_text == "好的，装载被拒绝了。"

    tool_step = [s for s in result.steps if s.tool_results][0]
    tool_result = tool_step.tool_results[0]
    assert tool_result.error is True
    assert tool_result.result["denied"] is True
    # 拒绝路径：什么都没装上
    assert CLOCK_SERVICE not in booted.ctx.services
    assert "now" not in booted.ctx.require(TOOLS_SERVICE).names
    booted.close()


def test_load_plugin_approved_path_actually_loads(tmp_path: Path):
    approver = ScriptedApprover([ApprovalDecision(True, "批准"), ApprovalDecision(True, "批准")])
    booted = _boot(
        tmp_path,
        script=[
            tool_call_reply("load_plugin", {"name": "clock"}),
            text_reply("已装载 clock。"),
        ],
        approval=approver,
    )
    result = booted.agent.ask("装一下 clock", session_id="s1")
    assert result.status == "done"
    assert CLOCK_SERVICE in booted.ctx.services
    assert "now" in booted.ctx.require(TOOLS_SERVICE).names
    booted.close()


def test_unload_plugin_requires_approval(tmp_path: Path):
    """装卸都标了 needs_approval；只读的 list 没有（与上面"拒绝即无副作用"配套）。"""
    booted = _boot(tmp_path)
    registry = booted.ctx.require(TOOLS_SERVICE)
    assert registry.resolve("load_plugin").definition.needs_approval is True
    assert registry.resolve("unload_plugin").definition.needs_approval is True
    assert registry.resolve("list_plugins").definition.needs_approval is False
    booted.close()


def test_list_plugins_does_not_need_approval(tmp_path: Path):
    """只读工具不打扰用户：不用审批就能列台账（模型可以放心自查）。"""
    approver = ScriptedApprover([])  # 一条审批都不该被问
    booted = _boot(
        tmp_path,
        script=[
            tool_call_reply("list_plugins", {}),
            text_reply("台账如上。"),
        ],
        approval=approver,
    )
    result = booted.agent.ask("列出插件", session_id="s1")
    assert result.status == "done"
    approver.assert_all_consumed()  # 0 条剧本、0 次询问
    tool_step = [s for s in result.steps if s.tool_results][0]
    assert tool_step.tool_results[0].error is False
    booted.close()


# ---------------------------------------------------------------------------
# 事件留痕
# ---------------------------------------------------------------------------


def test_load_and_unload_emit_events(tmp_path: Path):
    booted = _boot(tmp_path)
    seen: list[tuple[str, dict]] = []
    booted.ctx.on(PLUGIN_LOADED, lambda **kw: seen.append(("loaded", kw)))
    booted.ctx.on(PLUGIN_UNLOADED, lambda **kw: seen.append(("unloaded", kw)))

    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    service.load("clock")
    service.unload("clock")

    assert seen[0] == ("loaded", {"name": "clock", "status": "active"})
    assert seen[1] == ("unloaded", {"name": "clock"})
    booted.close()


def test_tool_calls_are_logged_in_session(tmp_path: Path):
    """装卸经过工具调用 → tool/result 事件进会话日志（模型动过什么有据可查）。"""
    booted = _boot(
        tmp_path,
        script=[
            tool_call_reply("load_plugin", {"name": "clock"}),
            text_reply("已装载。"),
        ],
        approval=ScriptedApprover([ApprovalDecision(True, "批准")]),
    )
    booted.agent.ask("装 clock", session_id="s1")
    opened = booted.agent.open("s1")
    types = [e.type for e in opened.session.events]
    assert "tool/result" in types
    tool_events = [e for e in opened.session.events if e.type == "tool/result"]
    assert tool_events[0].data["name"] == "load_plugin"
    assert tool_events[0].data["result"]["loaded"] == "clock"
    booted.close()


# ---------------------------------------------------------------------------
# 依赖退位：tools 服务重建时自扩展插件随之重装
# ---------------------------------------------------------------------------


def test_self_tools_survive_a_capability_swap(tmp_path: Path):
    """换 fs（工具用到的能力）时：**工具面与自扩展工具不跟着退位**。

    `_06` 的语义变化（拆分带来的好处）：`tools` 对 fs 只声明 `optional`——
    它本身不需要文件系统（只是"装新工具插件"那条路要读源码），而工具注册表是
    **装配级**的东西。所以换掉 fs 时：依赖 fs 的**工具插件**退位（tool-fs /
    tool-search），注册表与自扩展工具照常活着——恢复 fs 时它们自动回来。

    对比 `_05`：工具内容焊在 tools 里时，tools 不得不 `inject fs`，
    于是"换个文件系统"会顺手把整个工具面（含 list_plugins 这些管理工具）拆掉。
    """
    from plugins import local_fs_plugin

    booted = _boot(tmp_path)
    assert SELF_TOOLS_SERVICE in booted.ctx.services
    registry = booted.ctx.require(TOOLS_SERVICE)
    before = set(registry.names)
    assert {"read_file", "search_text"} <= before

    booted.ctx.unload_plugin("fs:MemoryFS")
    # 依赖 fs 的工具插件退位；底座与自扩展工具不动
    assert SELF_TOOLS_SERVICE in booted.ctx.services
    assert "tool-fs" in booted.ctx.pending_plugins
    assert "tool-search" in booted.ctx.pending_plugins
    assert {"list_plugins", "load_plugin", "unload_plugin"} <= set(registry.names)
    assert not {"read_file", "search_text"} & set(registry.names)   # 它们的工具已回卷

    booted.ctx.load_plugin(local_fs_plugin(tmp_path / "disk_ws"))
    assert set(registry.names) == before      # 能力回来，工具面原样恢复
    booted.close()


def test_plugin_factory_matches_whitelist_name(tmp_path: Path):
    """白名单名字与实际插件名一致（防止"名字对不上"的静默错装）。"""
    from plugins.self_tools import default_catalog

    catalog = default_catalog()
    assert set(catalog) == {
        "clock", "rpc", "webui",
        "tool-calc", "tool-fs", "tool-search", "tool-shell",   # `_06`
    }
    for name in ("clock", "webui", "rpc", "tool-calc", "tool-fs", "tool-search", "tool-shell"):
        assert catalog[name]().name == name, f"{name} 的工厂造出了别的插件"
