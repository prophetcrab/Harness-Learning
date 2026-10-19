"""工具底座验收：定义 / 注册表 / 管线 / 四个插座 / 管理面。

不碰真实文件与进程——只验底座本身的语义。
"""

from __future__ import annotations

from typing import Any

import pytest

from core import Context
from plugins.base.tools_admin import (
    TOOL_ADMIN_SERVICE,
    DuplicateToolError,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    UnknownToolError,
    tool,
    tools_admin_plugin,
    validate_arguments,
)

# ---------------------------------------------------------------------------
# 定义：schema 从签名推导
# ---------------------------------------------------------------------------


@tool("greet")
def greet(name: str, excited: bool = False) -> dict[str, Any]:
    """向某人问好。"""
    return {"hello": name, "excited": excited}


def test_schema_derived_from_signature():
    schema = greet.definition.schema()
    assert schema["name"] == "greet"
    assert schema["description"] == "向某人问好。"
    assert schema["parameters"]["name"]["type"] == "string"
    assert schema["parameters"]["name"]["required"] is True
    assert schema["parameters"]["excited"]["type"] == "boolean"
    assert schema["parameters"]["excited"]["required"] is False


def test_validate_fills_defaults_and_rejects_bad_input():
    args = validate_arguments(greet.definition, {"name": "世界"})
    assert args == {"name": "世界", "excited": False}

    with pytest.raises(ValueError, match="缺少必填"):
        validate_arguments(greet.definition, {})
    with pytest.raises(ValueError, match="类型不对"):
        validate_arguments(greet.definition, {"name": 123})
    with pytest.raises(ValueError, match="不认识参数"):
        validate_arguments(greet.definition, {"name": "x", "oops": 1})


def test_integer_rejects_bool():
    definition = ToolDefinition(
        name="count",
        description="",
        parameters={"n": __import__("plugins.base.tools_admin", fromlist=["ParamSpec"]).ParamSpec(type="integer", required=True)},
    )
    with pytest.raises(ValueError, match="布尔"):
        validate_arguments(definition, {"n": True})


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_register_owner_and_duplicate():
    registry = ToolRegistry()
    registry.register(greet.definition, greet.func, owner="tool-a")
    assert registry.owners() == {"tool-a": ["greet"]}
    assert registry.is_enabled("greet")

    with pytest.raises(DuplicateToolError, match="tool-a"):
        registry.register(greet.definition, greet.func, owner="tool-b")
    with pytest.raises(ValueError, match="署名"):
        registry.register(greet.definition, greet.func, owner="")


def test_unregister_is_identity_bound():
    registry = ToolRegistry()
    off = registry.register(greet.definition, greet.func, owner="tool-a")
    off()
    off()
    registry.register(greet.definition, greet.func, owner="tool-a")
    off()  # 旧句柄不动新注册
    assert registry.get("greet") is not None


def test_visible_excludes_disabled():
    registry = ToolRegistry()
    registry.register(greet.definition, greet.func, owner="tool-a")
    assert [d.name for d in registry.visible()] == ["greet"]
    registry.set_enabled("greet", False)
    assert registry.visible() == []
    assert registry.disabled == ["greet"]
    with pytest.raises(UnknownToolError):
        registry.set_enabled("nope", False)


# ---------------------------------------------------------------------------
# 管线 + 四个插座
# ---------------------------------------------------------------------------


def booted(**config) -> tuple[Context, Any]:
    ctx = Context()
    ctx.load_plugin(tools_admin_plugin(**config))
    return ctx, ctx.require(TOOL_ADMIN_SERVICE)


def register_echo(runtime, *, needs_approval: bool = False):
    @tool("echo", needs_approval=needs_approval)
    def echo(text: str) -> dict[str, Any]:
        """原样返回。"""
        return {"text": text}

    runtime.register(echo.definition, echo.func, owner="tool-echo")
    return echo


def test_execute_runs_the_tool_and_reports_duration():
    _ctx, runtime = booted()
    register_echo(runtime)
    result = runtime.execute("echo", {"text": "hi"})
    assert result.ok and result.content == {"text": "hi"}
    assert result.duration_ms >= 0


def test_unknown_or_disabled_tool_is_an_error_result():
    _ctx, runtime = booted()
    register_echo(runtime)
    assert runtime.execute("nope", {}).error_code == "TOOL_NOT_FOUND"

    runtime.set_enabled("echo", False)
    assert runtime.execute("echo", {}).error_code == "TOOL_NOT_FOUND"


def test_bad_arguments_are_an_error_result_not_an_exception():
    _ctx, runtime = booted()
    register_echo(runtime)
    result = runtime.execute("echo", {})
    assert result.status == "error" and result.error_code == "TOOL_BAD_ARGUMENTS"
    assert "缺少必填" in result.message


def test_pre_execute_can_deny():
    ctx, runtime = booted()
    register_echo(runtime)
    ctx.on("tools/pre-execute", lambda value, **kw: __import__("plugins.base.tools_admin", fromlist=["PreDecision"]).PreDecision("deny", "测试拒绝"))
    result = runtime.execute("echo", {"text": "hi"})
    assert result.status == "denied" and "测试拒绝" in result.message


def test_ask_tool_without_approval_is_denied():
    _ctx, runtime = booted()  # 没有 approval 服务 → AutoDeny
    register_echo(runtime, needs_approval=True)
    result = runtime.execute("echo", {"text": "hi"})
    assert result.status == "denied"


def test_ask_tool_with_approval_service():
    from plugins.base.tools_admin import AutoApprove

    ctx, runtime = booted(approval=AutoApprove())
    register_echo(runtime, needs_approval=True)
    assert runtime.execute("echo", {"text": "hi"}).ok


def test_execute_waterfall_wraps_the_call():
    ctx, runtime = booted()
    calls: list[str] = []

    @tool("boom")
    def boom() -> str:
        """失败。"""
        raise ValueError("工具内部炸了")

    runtime.register(boom.definition, boom.func, owner="tool-boom")

    def post(value, **kw):
        calls.append("post")
        return ToolResult(status="ok", content=f"包装过：{value.content}")

    ctx.on("tools/post-execute", post)
    result = runtime.execute("boom", {})
    assert calls == ["post"]
    assert result.ok


def test_tool_exception_becomes_error_result():
    _ctx, runtime = booted()

    @tool("broken")
    def broken() -> str:
        """抛错。"""
        raise RuntimeError("内部错误")

    runtime.register(broken.definition, broken.func, owner="tool-broken")
    result = runtime.execute("broken", {})
    assert result.status == "error" and result.error_code == "TOOL_EXCEPTION"
    assert "内部错误" in result.message


def test_result_event_is_emitted():
    ctx, runtime = booted()
    register_echo(runtime)
    seen: list[dict] = []
    ctx.on("tools/result", lambda tool, result, **kw: seen.append({"tool": tool, "ok": result.ok}))
    runtime.execute("echo", {"text": "hi"})
    assert seen == [{"tool": "echo", "ok": True}]


def test_tool_timeout():
    _ctx, runtime = booted()
    import time

    @tool("slow")
    def slow() -> str:
        """慢。"""
        time.sleep(0.5)
        return "done"

    runtime.register(slow.definition, slow.func, owner="tool-slow")
    runtime._pipeline._timeout = 0.05
    result = runtime.execute("slow", {})
    assert result.status == "timeout" and result.error_code == "TOOL_TIMEOUT"


# ---------------------------------------------------------------------------
# 内建的两件自我管理工具
# ---------------------------------------------------------------------------


def test_builtin_self_management_tools():
    _ctx, runtime = booted()
    names = {record.definition.name for record in runtime.entries().values()}
    assert {"list_tools", "install_tool"} <= names
    assert runtime.entries()["list_tools"].owner == "tools-admin"
    assert runtime.entries()["install_tool"].definition.needs_approval is True

    listed = runtime.execute("list_tools", {}).content
    assert any(item["name"] == "list_tools" for item in listed["tools"])


# ---------------------------------------------------------------------------
# 管理面
# ---------------------------------------------------------------------------


def test_admin_snapshot_groups_by_owner():
    ctx, _runtime = booted()
    register_echo(_runtime)
    admin = ctx.require("tool-admin")
    snap = admin.snapshot()
    owners = {group["owner"] for group in snap["groups"]}
    assert {"tools-admin", "tool-echo"} <= owners
    assert snap["counts"]["total"] >= 3


def test_admin_disable_enable_and_uninstall():
    ctx, runtime = booted()
    register_echo(runtime)
    admin = ctx.require("tool-admin")

    assert admin.disable("echo")["ok"] is True
    assert runtime.is_enabled("echo") is False
    assert admin.enable("echo")["ok"] is True
    assert runtime.is_enabled("echo") is True

    assert admin.install("nope")["ok"] is False
    assert admin.uninstall("tool-echo")["ok"] is False  # 不是插件，只是署名
