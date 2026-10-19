"""approval-terminal 验收：三种策略 + 非交互环境 fail-closed。"""

from __future__ import annotations

import sys

import pytest

from core import Context
from plugins.base.tools_admin import ToolApprovalRequest
from plugins.capability.approval.terminal import (
    APPROVAL_SERVICE,
    TerminalApproval,
    approval_terminal_plugin,
)


def tool_request() -> ToolApprovalRequest:
    return ToolApprovalRequest(tool="shell", arguments={"command": "ls"}, session_id="s1")


def path_request():
    from plugins.base.fs import ApprovalRequest

    return ApprovalRequest(
            operation="write", path="/tmp/x", session_id="s1", exists=True, risky=False
        )


def test_allow_and_deny_policies():
    assert TerminalApproval("allow").ask(tool_request()) is True
    assert TerminalApproval("deny").ask(tool_request()) is False


def test_ask_policy_denies_without_a_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert TerminalApproval("ask").ask(tool_request()) is False


def test_ask_policy_reads_y(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    approval = TerminalApproval("ask")
    assert approval.ask(tool_request()) is True
    assert approval.asked == ["shell"]
    assert "需要审批" in capsys.readouterr().err


def test_ask_policy_reads_n(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert TerminalApproval("ask").ask(tool_request()) is False


def test_describe_handles_tool_and_path_requests():
    from plugins.capability.approval.terminal import describe

    assert "shell" in describe(tool_request())
    assert "write" in describe(path_request())


def test_plugin_provides_the_service():
    ctx = Context()
    ctx.load_plugin(approval_terminal_plugin(policy="allow"))
    service = ctx.require(APPROVAL_SERVICE)
    assert service.ask(tool_request()) is True


def test_invalid_policy_fails_loud():
    with pytest.raises(ValueError, match="审批策略"):
        TerminalApproval("sometimes")


def test_approval_installed_after_tools_still_works():
    """审批通道可以排在工具面之后装载（装载顺序无关）。

    实测 bug：tools-admin 在 setup 时抓了一份 approval 服务，之后装上的
    审批通道永远不生效——`--yes` 给了 allow，shell 仍然全部 denied。
    """
    from plugins.base.tools_admin import TOOL_ADMIN_SERVICE, tool, tools_admin_plugin

    ctx = Context()
    ctx.load_plugin(tools_admin_plugin())          # 先装工具面
    ctx.load_plugin(approval_terminal_plugin(policy="allow"))   # 后装审批

    @tool("needs_yes", needs_approval=True)
    def needs_yes() -> str:
        """需要审批的工具。"""
        return "ok"

    runtime = ctx.require(TOOL_ADMIN_SERVICE)
    runtime.register(needs_yes.definition, needs_yes.func, owner="test")
    result = runtime.execute("needs_yes", {})
    assert result.status == "ok"


def test_fs_sandbox_sees_approval_installed_later():
    import tempfile
    from pathlib import Path

    from plugins.base.fs import ApprovalRequest, fs_plugin
    from plugins.base.tools_admin import tools_admin_plugin
    from plugins.base.workspace import workspace_plugin
    from plugins.capability.fs.sandbox import fs_sandbox_plugin

    root = Path(tempfile.mkdtemp())
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=root))
    ctx.load_plugin(fs_plugin())
    ctx.load_plugin(tools_admin_plugin())
    ctx.load_plugin(fs_sandbox_plugin(mode="changes"))   # 先装沙箱
    ctx.load_plugin(approval_terminal_plugin(policy="allow"))  # 后装审批

    # 直接问沙箱策略：应该在用时看到 allow
    policy = ctx.require("fs-sandbox")
    request = ApprovalRequest(
        operation="write", path=str(root / "x"), session_id="s", exists=False, risky=False
    )
    policy.approve(request)  # 不抛 = 放行
    assert APPROVAL_SERVICE
