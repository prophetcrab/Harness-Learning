"""六个工具插件 + 两个 fs 策略 + 三个 proc 后端验收。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from core import Context
from plugins.base.fs import AutoApprove, AutoDeny, fs_plugin
from plugins.base.proc import PROC_SERVICE, detect_dialect, proc_plugin
from plugins.base.tools_admin import TOOL_ADMIN_SERVICE, ToolResult, tools_admin_plugin
from plugins.base.workspace import workspace_plugin
from plugins.capability.fs.observation_policy import fs_observation_policy_plugin
from plugins.capability.fs.sandbox import fs_sandbox_plugin
from plugins.capability.proc.backends import (
    proc_git_bash_plugin,
    proc_posix_plugin,
    proc_scripted_plugin,
)
from plugins.capability.tools.ask_user import tool_ask_user_plugin
from plugins.capability.tools.fs import tool_fs_plugin
from plugins.capability.tools.fs_search import tool_fs_search_plugin
from plugins.capability.tools.math import tool_math_plugin
from plugins.capability.tools.shell import tool_shell_plugin
from plugins.capability.tools.web_search import tool_web_search_plugin

# ---------------------------------------------------------------------------
# 装配夹具
# ---------------------------------------------------------------------------


def make_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "b.py").write_text("print('b')\n", encoding="utf-8")
    return root


def boot(
    tmp_path: Path,
    *,
    sandbox: str | None = "changes",
    approval=None,
    scripted: list[dict[str, Any]] | None = None,
    web_transport=None,
    asker=None,
) -> Context:
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path)))
    ctx.load_plugin(fs_plugin())
    ctx.load_plugin(proc_plugin())
    ctx.load_plugin(proc_scripted_plugin(results=scripted or []))
    ctx.load_plugin(tools_admin_plugin(approval=approval))
    if sandbox is not None:
        ctx.load_plugin(fs_sandbox_plugin(mode=sandbox, approval=approval))
        ctx.load_plugin(fs_observation_policy_plugin())
    ctx.load_plugin(tool_math_plugin())
    ctx.load_plugin(tool_fs_plugin())
    ctx.load_plugin(tool_shell_plugin())
    ctx.load_plugin(tool_fs_search_plugin())
    ctx.load_plugin(
        tool_web_search_plugin(
            search_url="https://search.example/api",
            transport=web_transport or (lambda url, timeout: "{}"),
        )
    )
    ctx.load_plugin(tool_ask_user_plugin(asker=asker))
    return ctx


def exec_tool(ctx: Context, name: str, args: dict[str, Any] | None = None) -> ToolResult:
    return ctx.require(TOOL_ADMIN_SERVICE).execute(name, args or {})


# ---------------------------------------------------------------------------
# tool-math
# ---------------------------------------------------------------------------


def test_calc_evaluates_expressions(tmp_path):
    ctx = boot(tmp_path)
    assert exec_tool(ctx, "calc", {"expression": "2 * (3 + 4)"}).content["value"] == 14
    assert exec_tool(ctx, "calc", {"expression": "sqrt(16) + pi"}).content["value"] == pytest.approx(4 + 3.141592653589793)


def test_calc_rejects_code_execution(tmp_path):
    ctx = boot(tmp_path)
    for expression in ("__import__('os').system('echo x')", "open('x')", "lambda: 1"):
        result = exec_tool(ctx, "calc", {"expression": expression})
        assert result.status == "error", expression


def test_math_function(tmp_path):
    ctx = boot(tmp_path)
    assert exec_tool(ctx, "math", {"name": "factorial", "args": [5]}).content["value"] == 120
    assert exec_tool(ctx, "math", {"name": "gcd", "args": [12, 18]}).content["value"] == 6
    assert exec_tool(ctx, "math", {"name": "nope", "args": [1]}).status == "error"


# ---------------------------------------------------------------------------
# tool-fs + fs-sandbox + fs-observation-policy
# ---------------------------------------------------------------------------


def test_read_file_and_list_files(tmp_path):
    ctx = boot(tmp_path)
    result = exec_tool(ctx, "read_file", {"path": "a.txt"})
    assert result.ok
    assert [line["text"] for line in result.content["lines"]] == ["hello", "world"]
    assert result.content["total_lines"] == 2

    listing = exec_tool(ctx, "list_files", {"path": "."})
    assert {item["path"] for item in listing.content} >= {"a.txt", "sub"}


def test_write_requires_reading_first(tmp_path):
    """读后写策略：没读过的已存在文件不能改。"""
    ctx = boot(tmp_path, approval=AutoApprove())
    blocked = exec_tool(ctx, "write_file", {"path": "a.txt", "content": "新内容"})
    assert blocked.status == "error" and "必须先读" in blocked.message

    exec_tool(ctx, "read_file", {"path": "a.txt"})
    allowed = exec_tool(ctx, "write_file", {"path": "a.txt", "content": "新内容"})
    assert allowed.ok
    assert (ctx.require("workspace").root / "a.txt").read_text(encoding="utf-8") == "新内容"


def test_new_file_needs_no_prior_read(tmp_path):
    ctx = boot(tmp_path, approval=AutoApprove())
    result = exec_tool(ctx, "write_file", {"path": "new.txt", "content": "x"})
    assert result.ok and result.content["created"] is True


def test_edit_file_replaces_text(tmp_path):
    ctx = boot(tmp_path, approval=AutoApprove())
    exec_tool(ctx, "read_file", {"path": "a.txt"})
    result = exec_tool(ctx, "edit_file", {"path": "a.txt", "old": "world", "new": "there"})
    assert result.ok and result.content["replacements"] == 1
    assert "there" in exec_tool(ctx, "read_file", {"path": "a.txt"}).content["lines"][1]["text"]


def test_sandbox_mode_changes_needs_approval(tmp_path):
    deny = AutoDeny()
    # changes：写要审批 → 被拒
    ctx = boot(tmp_path, sandbox="changes", approval=deny)
    assert exec_tool(ctx, "write_file", {"path": "x.txt", "content": "1"}).status == "error"

    # free：不审批 → 放行
    ctx_free = boot(tmp_path, sandbox="free", approval=deny)
    assert exec_tool(ctx_free, "write_file", {"path": "y.txt", "content": "1"}).ok


def test_sandbox_read_only_mode_requires_approval_for_reads(tmp_path):
    ctx = boot(tmp_path, sandbox="all", approval=AutoDeny())
    assert exec_tool(ctx, "read_file", {"path": "a.txt"}).status == "error"

    ctx_ok = boot(tmp_path, sandbox="all", approval=AutoApprove())
    assert exec_tool(ctx_ok, "read_file", {"path": "a.txt"}).ok


def test_sandbox_risky_asks_once_per_session(tmp_path):
    asked: list[str] = []

    class Counting:
        def ask(self, request):
            asked.append(request.path)
            return True

    ctx = boot(tmp_path, sandbox="risky", approval=Counting())
    exec_tool(ctx, "read_file", {"path": "a.txt"})
    first = exec_tool(ctx, "write_file", {"path": "a.txt", "content": "1"})
    second = exec_tool(ctx, "write_file", {"path": "a.txt", "content": "2"})
    assert first.ok and second.ok
    assert len(asked) == 1  # 同会话相同请求只问一次


def test_path_escape_is_rejected(tmp_path):
    ctx = boot(tmp_path, sandbox="free")
    result = exec_tool(ctx, "read_file", {"path": "../outside.txt"})
    assert result.status == "error"


# ---------------------------------------------------------------------------
# tool-shell + proc
# ---------------------------------------------------------------------------


def test_shell_uses_scripted_backend(tmp_path):
    ctx = boot(
        tmp_path,
        scripted=[{"status": "ok", "exit_code": 0, "stdout": "hi\n", "stderr": ""}],
        approval=AutoApprove(),
    )
    result = exec_tool(ctx, "shell", {"command": "echo hi"})
    assert result.ok and result.content["stdout"] == "hi\n"


@pytest.mark.skipif(sys.platform != "win32", reason="方言检查以 Git Bash 后端为基准")
def test_shell_rejects_wrong_dialect(tmp_path):
    """方言写错 → proc 在执行前拒绝（不把 cmd 命令丢给 bash）。"""
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path)))
    ctx.load_plugin(proc_plugin())
    ctx.load_plugin(proc_git_bash_plugin())
    ctx.load_plugin(tools_admin_plugin(approval=AutoApprove()))
    ctx.load_plugin(tool_shell_plugin())

    result = exec_tool(ctx, "shell", {"command": "dir /b"})
    assert result.status == "error"
    assert "cmd" in result.message and "posix" in result.message


def test_shell_needs_approval(tmp_path):
    ctx = boot(tmp_path, scripted=[{"status": "ok", "stdout": "x"}], approval=AutoDeny())
    result = exec_tool(ctx, "shell", {"command": "echo x"})
    assert result.status == "denied"
    assert ctx.require(PROC_SERVICE) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Git Bash 只在 Windows 上验")
def test_git_bash_backend_runs_real_command(tmp_path):
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path)))
    ctx.load_plugin(proc_plugin())
    ctx.load_plugin(proc_git_bash_plugin())
    proc = ctx.require(PROC_SERVICE)
    assert proc.select() is not None
    result = proc.run("echo 你好 && printf 'a\\nb\\n'", cwd=ctx.require("workspace").root)
    assert result.status == "ok"
    assert result.stdout == "你好\na\nb\n"  # 中文不乱码、换行已归一


@pytest.mark.skipif(sys.platform != "win32", reason="Git Bash 只在 Windows 上验")
def test_git_bash_reports_missing_command(tmp_path):
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path)))
    ctx.load_plugin(proc_plugin())
    ctx.load_plugin(proc_git_bash_plugin())
    result = ctx.require(PROC_SERVICE).run("definitely-not-a-command", cwd=ctx.require("workspace").root)
    assert result.status == "error" and result.exit_code != 0


def test_proc_backends_listed_and_ordered(tmp_path):
    ctx = Context()
    ctx.load_plugin(proc_plugin())
    ctx.load_plugin(proc_scripted_plugin())
    ctx.load_plugin(proc_posix_plugin())
    proc = ctx.require(PROC_SERVICE)
    assert set(proc.backends()) == {"scripted", "posix"}
    # posix 在这个平台上 available() 为 False → scripted 被选中
    assert proc.select().name == "scripted"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("dir /b", "cmd"),
        ("copy a.txt b.txt", "cmd"),
        ("echo %PATH%", "cmd"),
        ("Get-ChildItem -Path .", "powershell"),
        ("$env:PATH", "powershell"),
        ("ls -la", "posix"),
        ("grep -r foo .", "posix"),
        ("echo $HOME", "posix"),
        ("cd somewhere", "unknown"),
        ("mkdir x", "unknown"),
    ],
)
def test_dialect_detection(command, expected):
    assert detect_dialect(command) == expected


# ---------------------------------------------------------------------------
# tool-fs-search
# ---------------------------------------------------------------------------


def test_glob_and_grep(tmp_path):
    ctx = boot(tmp_path, sandbox="free")
    matches = exec_tool(ctx, "glob", {"pattern": "**/*.py"}).content["matches"]
    assert matches == ["sub/b.py"]

    hits = exec_tool(ctx, "grep", {"pattern": "print", "include": "*.py"}).content["hits"]
    assert hits == [{"path": "sub/b.py", "line": 1, "text": "print('b')"}]


def test_grep_bad_regex_is_error(tmp_path):
    ctx = boot(tmp_path, sandbox="free")
    assert exec_tool(ctx, "grep", {"pattern": "["}).status == "error"


# ---------------------------------------------------------------------------
# tool-web-search
# ---------------------------------------------------------------------------


def test_web_fetch_and_search(tmp_path):
    def transport(url: str, timeout: float) -> str:
        if "search" in url:
            return '{"results": [{"title": "T", "url": "https://x", "content": "摘要"}]}'
        return "页面正文"

    ctx = boot(tmp_path, web_transport=transport)
    fetched = exec_tool(ctx, "web_fetch", {"url": "https://example.com/page"})
    assert fetched.content["content"] == "页面正文"

    found = exec_tool(ctx, "web_search", {"query": "测试", "limit": 3})
    assert found.content["results"][0]["title"] == "T"


def test_web_rejects_non_http(tmp_path):
    ctx = boot(tmp_path)
    assert exec_tool(ctx, "web_fetch", {"url": "file:///etc/passwd"}).status == "error"


def test_web_search_without_endpoint_fails_loud(tmp_path):
    ctx = Context()
    ctx.load_plugin(proc_plugin())
    ctx.load_plugin(tools_admin_plugin())
    ctx.load_plugin(tool_web_search_plugin())
    result = exec_tool(ctx, "web_search", {"query": "x"})
    assert result.status == "error" and "没有配置搜索端点" in result.message


# ---------------------------------------------------------------------------
# tool-ask-user
# ---------------------------------------------------------------------------


def test_ask_user_requires_two_options(tmp_path):
    ctx = boot(tmp_path, asker=lambda q, o, c: o[0])
    result = exec_tool(ctx, "ask_user", {"question": "选哪个？", "options": ["只有一个"]})
    assert result.status == "error" and "两个选项" in result.message


def test_ask_user_returns_answer(tmp_path):
    asked: list[tuple] = []

    def asker(question, options, allow_custom):
        asked.append((question, options, allow_custom))
        return options[1]

    ctx = boot(tmp_path, asker=asker)
    result = exec_tool(
        ctx, "ask_user", {"question": "用哪个？", "options": ["A", "B"], "allow_custom": True}
    )
    assert result.ok and result.content["answer"] == "B"
    assert asked == [("用哪个？", ("A", "B"), True)]


def test_ask_user_without_channel_fails_loud(tmp_path):
    ctx = boot(tmp_path, asker=None)
    result = exec_tool(ctx, "ask_user", {"question": "?", "options": ["A", "B"]})
    assert result.status == "error" and "提问通道" in result.message


# ---------------------------------------------------------------------------
# 插件装卸
# ---------------------------------------------------------------------------


def test_unloading_tool_plugin_removes_its_tools(tmp_path):
    ctx = boot(tmp_path)
    assert ctx.require(TOOL_ADMIN_SERVICE).get("calc") is not None
    ctx.unload_plugin("tool-math")
    assert ctx.require(TOOL_ADMIN_SERVICE).get("calc") is None
    assert exec_tool(ctx, "calc", {"expression": "1+1"}).status == "error"


def test_disabling_tool_hides_it_from_model(tmp_path):
    ctx = boot(tmp_path)
    tools = ctx.require(TOOL_ADMIN_SERVICE)
    assert "calc" in [spec["name"] for spec in tools.specs()]
    tools.set_enabled("calc", False)
    assert "calc" not in [spec["name"] for spec in tools.specs()]
