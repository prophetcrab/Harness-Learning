"""_06_Subprocess_Seam 的接线测试：shell 工具跑完整会话（全离线，FakeLLM 驱动）。

验收映射（本阶段 README）：
- 「捕获 stdout / stderr / 退出码」→ 命令结果经工具进日志、进模型历史；
- 「超时能被处理」→ 超时被渲染成 SHELL_TIMEOUT 结构化结果回给模型、不炸循环；
- 「工具只依赖抽象」→ 同一段对话在 LocalSubprocess / ScriptedSubprocess 下
  校验到同一种请求形状（cwd/期限由组装解析），且剧本模式下零真实进程。

运行：cd P2_Coding/_06_Subprocess_Seam && python -m pytest -q
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from context import collect_runtime_context, open_context_harness
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.session import JsonlStore
from harness.tools import AutoApprove, ScriptedApprover
from harness.tools.approval import ApprovalDecision
from providers import (
    FS_CAPABILITY,
    SHELL_NONZERO_EXIT,
    SHELL_TIMEOUT,
    SUBPROCESS_CAPABILITY,
    CommandResult,
    LocalFS,
    LocalSubprocess,
    MemoryFS,
    ScriptedSubprocess,
    ServiceContainer,
    build_toolbox,
)

PYTHON = shutil.which("python") or sys.executable


def py(code: str) -> str:
    return f'"{PYTHON}" -c "{code}"'


def _session(tmp_path: Path, session_id: str, provider, tool_registry, approval):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)  # shell 的工作目录要真实存在
    return open_context_harness(
        session_id,
        provider=provider,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=approval,
        context_source=lambda: collect_runtime_context(tmp_path / "ws", platform_name="DemoOS"),
        tool_registry=tool_registry,
    )


# =========================================================================
# 1) 真实执行进日志、进历史
# =========================================================================


def test_real_command_result_reaches_log_and_history(tmp_path: Path):
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("print(6*7)")}),
            text_reply("输出是 42。"),
        ]
    )
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"), LocalSubprocess(), workspace=tmp_path / "ws"
    )
    ctx = _session(tmp_path, "real", inner, registry, AutoApprove())
    result = ctx.harness.send("跑一下")
    inner.assert_all_consumed()

    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is False
    assert tool_result.result["exit_code"] == 0
    assert tool_result.result["stdout"].strip() == "42"

    # 模型历史里有命令输出（进入下一步请求的内容）
    tool_messages = [m for m in ctx.harness.history if m.role == "tool"]
    assert any("42" in m.content for m in tool_messages)

    # 落盘日志里也有
    events, _ = JsonlStore(tmp_path / "sessions").load("real")
    results = [e.data for e in events if e.type == "tool/result"]
    assert results and results[0]["result"]["stdout"].strip() == "42"


def test_cwd_is_workspace(tmp_path: Path):
    """shell 的工作目录 = 会话工作区（显式解析，模型不可改）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("import os; print(os.getcwd())")}),
            text_reply("拿到了目录。"),
        ]
    )
    registry = build_toolbox(LocalFS(workspace), LocalSubprocess(), workspace=workspace)
    ctx = _session(tmp_path, "cwd", inner, registry, AutoApprove())
    result = ctx.harness.send("我在哪")
    inner.assert_all_consumed()

    printed = result.steps[0].tool_results[0].result["stdout"].strip()
    assert printed.lower() == str(workspace).lower()


def test_workspace_file_visible_to_shell(tmp_path: Path):
    """文件接缝与命令接缝共享同一个工作区：写进去的文件命令看得见。"""
    workspace = tmp_path / "ws"
    inner = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "marker.txt", "content": "seam"}),
            text_reply("写好了。"),
            tool_call_reply(
                "shell",
                {"command": py("print(open('marker.txt', encoding='utf-8').read())")},
            ),
            text_reply("看到了。"),
        ]
    )
    registry = build_toolbox(LocalFS(workspace), LocalSubprocess(), workspace=workspace)
    ctx = _session(tmp_path, "shared", inner, registry, AutoApprove())
    ctx.harness.send("写文件")
    result = ctx.harness.send("用命令读它")
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].result["stdout"].strip() == "seam"


# =========================================================================
# 2) 失败路径（非零 / 超时）都是给模型的输入
# =========================================================================


def test_nonzero_exit_reaches_model_as_error(tmp_path: Path):
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("import sys; sys.exit(5)")}),
            text_reply("命令失败了，我知道了。"),
        ]
    )
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"), LocalSubprocess(), workspace=tmp_path / "ws"
    )
    ctx = _session(tmp_path, "nonzero", inner, registry, AutoApprove())
    result = ctx.harness.send("跑个会失败的")
    inner.assert_all_consumed()

    assert result.status == "done"  # 循环没被炸
    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is True
    assert tool_result.result["code"] == SHELL_NONZERO_EXIT
    assert tool_result.result["exit_code"] == 5


def test_timeout_reaches_model_as_error(tmp_path: Path):
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("import time; time.sleep(30)")}),
            text_reply("命令超时了，我换个方式。"),
        ]
    )
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"),
        LocalSubprocess(),
        workspace=tmp_path / "ws",
        shell_timeout=0.6,
    )
    ctx = _session(tmp_path, "timeout", inner, registry, AutoApprove())
    result = ctx.harness.send("跑个会卡住的")
    inner.assert_all_consumed()

    assert result.status == "done"
    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is True
    assert tool_result.result["code"] == SHELL_TIMEOUT

    events, _ = JsonlStore(tmp_path / "sessions").load("timeout")
    assert any(e.type == "tool/result" and e.data["is_error"] for e in events)


def test_spawn_failure_reaches_model(tmp_path: Path):
    """工作目录不存在（基础设施失败）也变成给模型的结构化结果。"""
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": "echo x"}),
            text_reply("环境有问题。"),
        ]
    )
    registry = build_toolbox(
        MemoryFS(), LocalSubprocess(), workspace=str(tmp_path / "missing-dir")
    )
    ctx = _session(tmp_path, "spawn", inner, registry, AutoApprove())
    result = ctx.harness.send("跑一下")
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].result["code"] == "SHELL_SPAWN_FAILED"


# =========================================================================
# 3) 审批：shell 是危险工具，默认要过审批；拒绝则命令不执行
# =========================================================================


def test_shell_requires_approval_and_denial_blocks_execution(tmp_path: Path):
    marker = tmp_path / "ws" / "should-not-exist.txt"
    from harness.tools.approval import AutoDeny

    inner = FakeLLM(
        [
            tool_call_reply(
                "shell",
                {"command": py(f"open(r'{marker}', 'w').close()")},
            ),
            text_reply("那我不执行了。"),
        ]
    )
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"), LocalSubprocess(), workspace=tmp_path / "ws"
    )
    ctx = _session(tmp_path, "deny", inner, registry, AutoDeny("测试拒绝"))
    result = ctx.harness.send("写个标记文件")
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].error is True
    assert not marker.exists()  # ★ 命令真的没跑


def test_approval_script_matches_exactly(tmp_path: Path):
    """审批剧本精确匹配：shell 恰好被问一次。"""
    approver = ScriptedApprover([ApprovalDecision(True, "批准命令")])
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("print('go')")}),
            text_reply("跑完了。"),
        ]
    )
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"), LocalSubprocess(), workspace=tmp_path / "ws"
    )
    ctx = _session(tmp_path, "approve", inner, registry, approver)
    ctx.harness.send("跑一下")
    inner.assert_all_consumed()
    assert approver.request_count == 1
    approver.assert_all_consumed()


# =========================================================================
# 4) 双 provider 对照：同一个会话剧本，两种实现都被正确消费
# =========================================================================


def test_scripted_provider_runs_no_real_process(tmp_path: Path):
    """剧本 provider：描述一个不可能的命令，会话照常走完——零真实进程。"""
    scripted = ScriptedSubprocess(
        [CommandResult(0, "pretend output\n"), CommandResult(1, "", "pretend failure\n")]
    )
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": "this-command-cannot-exist --x"}),
            text_reply("好。"),
            tool_call_reply("shell", {"command": "also-impossible"}),
            text_reply("知道了。"),
        ]
    )
    registry = build_toolbox(
        MemoryFS(), scripted, workspace="/fake/ws", shell_timeout=9
    )
    ctx = _session(tmp_path, "scripted", inner, registry, AutoApprove())
    first = ctx.harness.send("跑第一个")
    second = ctx.harness.send("跑第二个")
    inner.assert_all_consumed()
    scripted.assert_all_consumed()

    assert first.steps[0].tool_results[0].error is False
    assert second.steps[0].tool_results[0].result["code"] == SHELL_NONZERO_EXIT
    # 请求形状由组装决定（两种 provider 收到同样的 cwd/期限）
    assert all(r.cwd == str(Path("/fake/ws")) for r in scripted.requests)
    assert all(r.timeout == 9 for r in scripted.requests)


def test_service_container_resolves_both_capabilities():
    """两个能力各占一槽：fs 与 subprocess 互不影响（单槽语义是"每能力一个"）。"""
    services = ServiceContainer()
    services.register(FS_CAPABILITY, MemoryFS())
    services.register(SUBPROCESS_CAPABILITY, ScriptedSubprocess([]))
    fs = services.resolve(FS_CAPABILITY)
    shell_svc = services.resolve(SUBPROCESS_CAPABILITY)
    assert isinstance(fs, MemoryFS)
    assert isinstance(shell_svc, ScriptedSubprocess)
    # 同一能力重复注册仍报错
    import pytest

    with pytest.raises(ValueError):
        services.register(SUBPROCESS_CAPABILITY, LocalSubprocess())


# =========================================================================
# 5) 与其他机制共存：提示词/日志照常
# =========================================================================


def test_prompt_trace_unaffected(tmp_path: Path):
    from context import effective_system_prompt, latest_prompt_trace, project
    from prompt import rebuild_text

    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("print('x')")}),
            text_reply("好。"),
        ]
    )
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"), LocalSubprocess(), workspace=tmp_path / "ws"
    )
    ctx = _session(tmp_path, "trace", inner, registry, AutoApprove())
    ctx.harness.send("跑")
    inner.assert_all_consumed()

    stored, _ = JsonlStore(tmp_path / "sessions").load("trace")
    assert rebuild_text(latest_prompt_trace(stored)) == effective_system_prompt(stored)
    roles = [m.role for m in project(stored)]
    assert roles[0] == "system" and "tool" in roles
