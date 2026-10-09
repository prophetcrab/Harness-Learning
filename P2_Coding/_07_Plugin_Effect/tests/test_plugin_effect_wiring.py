"""_07_Plugin_Effect 的接线测试：插件装配跑完整会话（全离线，FakeLLM 驱动）。

验收映射（本阶段 README）：
- 「setup 返回 disposer / 注册即 effect」→ 每个 install_* 在 ctx 里留台账；
- 「卸载后注册物自动撤销（回卷）」→ 卸载单个插件只撤销它装的部分；整体卸载全清；
  卸载后**新建会话不再拿到能力**（工具面随槽位消失）；
- 「重复注册报错」→ 装载两份 fs 插件时槽位冲突；缺依赖装载 fail loud。

运行：cd P2_Coding/_07_Plugin_Effect && python -m pytest -q
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from context import collect_runtime_context, open_context_harness
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.session import JsonlStore
from harness.tools import AutoApprove
from kernel import Context, DuplicateSlotError, load_plugins
from providers import (
    FS_CAPABILITY,
    SUBPROCESS_CAPABILITY,
    TOOLBOX_CAPABILITY,
    CommandResult,
    LocalFS,
    LocalSubprocess,
    MemoryFS,
    ScriptedSubprocess,
    boot_toolbox,
    make_fs_plugin,
    make_subprocess_plugin,
    make_toolbox_plugin,
)

PYTHON = shutil.which("python") or sys.executable


def py(code: str) -> str:
    return f'"{PYTHON}" -c "{code}"'


def _boot_story_plugins(workspace: Path, shell: object | None = None) -> Context:
    """主故事用的三插件装载（fs + 可选 subprocess + toolbox）。"""
    ctx = Context()
    plugins: list[object] = [make_fs_plugin(LocalFS(workspace))]
    if shell is not None:
        plugins.append(make_subprocess_plugin(shell))
    plugins.append(make_toolbox_plugin(workspace=workspace, shell_timeout=10))
    load_plugins(ctx, plugins)
    return ctx


# =========================================================================
# 1) 插件装配：槽位/台账/工具面，以及一次完整会话
# =========================================================================


def test_boot_toolbox_registers_slots_and_ledger():
    shell = ScriptedSubprocess([CommandResult(0, "ok\n")])
    ctx, toolbox = boot_toolbox(MemoryFS(), shell, workspace="/demo/ws")
    assert ctx.slots == [FS_CAPABILITY, SUBPROCESS_CAPABILITY, TOOLBOX_CAPABILITY]
    assert ctx.effect_names == [
        "plugin:fs:MemoryFS",
        "plugin:subprocess:ScriptedSubprocess",
        "plugin:toolbox",
    ]
    assert "shell" in toolbox.names


def test_toolbox_requires_fs_fail_loud():
    """工具面依赖 fs 槽位：没装 fs 插件就装 toolbox → 装载时 fail loud。"""
    ctx = Context()
    with pytest.raises(KeyError, match="未提供"):
        load_plugins(ctx, [make_toolbox_plugin(workspace="/w")])
    assert ctx.slots == []  # 失败的装载没留下东西


def test_duplicate_fs_slot_fail_loud():
    """两份 fs 插件冲突（单槽语义在插件层依然成立）。"""
    ctx = Context()
    load_plugins(ctx, [make_fs_plugin(MemoryFS())])
    with pytest.raises(DuplicateSlotError):
        load_plugins(ctx, [make_fs_plugin(MemoryFS())])
    # 旧的还在，新的被拒——不静默顶替
    assert FS_CAPABILITY in ctx.slots


def test_session_runs_on_plugin_assembled_toolbox(tmp_path: Path):
    """整场会话跑在"插件装载出的工具面"上：命令 + 文件都好使。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    ctx = _boot_story_plugins(workspace, shell=LocalSubprocess())
    registry = ctx.require(TOOLBOX_CAPABILITY)

    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": py("print(21*2)")}),
            text_reply("输出 42。"),
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hi"}),
            text_reply("写好了。"),
        ]
    )
    harness_ctx = open_context_harness(
        "plugins",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
        tool_registry=registry,
    )
    first = harness_ctx.harness.send("跑命令")
    harness_ctx.harness.send("写文件")
    inner.assert_all_consumed()

    assert first.steps[0].tool_results[0].result["stdout"].strip() == "42"
    assert (workspace / "notes" / "a.txt").read_text(encoding="utf-8") == "hi"

    events, _ = JsonlStore(tmp_path / "sessions").load("plugins")
    assert any(e.type == "tool/result" and e.data["name"] == "shell" for e in events)


# =========================================================================
# 2) 卸载回卷：单插件 / 整体；卸载后能力不可用
# =========================================================================


def test_unload_toolbox_only_removes_its_slot():
    shell = ScriptedSubprocess([CommandResult(0, "ok\n")])
    ctx, _ = boot_toolbox(MemoryFS(), shell, workspace="/demo/ws")
    ctx.unload("plugin:toolbox")
    assert TOOLBOX_CAPABILITY not in ctx.slots
    assert FS_CAPABILITY in ctx.slots and SUBPROCESS_CAPABILITY in ctx.slots  # 其余的没动


def test_unload_all_leaves_clean_context():
    shell = ScriptedSubprocess([CommandResult(0, "ok\n")])
    ctx, _ = boot_toolbox(MemoryFS(), shell, workspace="/demo/ws")
    for name in reversed(ctx.effect_names):
        ctx.unload(name)
    assert ctx.slots == []
    assert ctx.effect_names == []


def test_toolbox_not_available_after_unload():
    """卸载后想再拿工具面：require fail loud——"撤销"是真实生效的。"""
    shell = ScriptedSubprocess([CommandResult(0, "ok\n")])
    ctx, _ = boot_toolbox(MemoryFS(), shell, workspace="/demo/ws")
    ctx.unload("plugin:toolbox")
    with pytest.raises(KeyError):
        ctx.require(TOOLBOX_CAPABILITY)


def test_reload_after_unload_works():
    """卸载后可重装（同一插件装第二遍）。"""
    shell = ScriptedSubprocess([CommandResult(0, "ok\n")])
    ctx, first_toolbox = boot_toolbox(MemoryFS(), shell, workspace="/demo/ws")
    ctx.unload("plugin:toolbox")
    load_plugins(ctx, [make_toolbox_plugin(workspace="/demo/ws")])
    second_toolbox = ctx.require(TOOLBOX_CAPABILITY)
    assert second_toolbox is not first_toolbox  # 新装的是新对象
    assert second_toolbox.names == first_toolbox.names


def test_session_keeps_working_after_ctx_unload(tmp_path: Path):
    """会话已经拿到工具面对象——ctx 卸载不影响已开跑的会话（引用而非寄生）。

    这是刻意的语义：卸载管理的是"装配台账"，不是"收回已发出去的对象"。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    ctx = _boot_story_plugins(workspace, shell=ScriptedSubprocess([CommandResult(0, "ok\n")]))
    registry = ctx.require(TOOLBOX_CAPABILITY)
    for name in reversed(ctx.effect_names):
        ctx.unload(name)

    inner = FakeLLM([text_reply("你好。")])
    harness_ctx = open_context_harness(
        "after-unload",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
        tool_registry=registry,  # 已摘下的工具面对象，仍然可用
    )
    result = harness_ctx.harness.send("你好")
    inner.assert_all_consumed()
    assert result.status == "done"


# =========================================================================
# 3) 与其他机制的共存：提示词/日志照常
# =========================================================================


def test_prompt_trace_unaffected_by_plugin_boot(tmp_path: Path):
    from context import effective_system_prompt, latest_prompt_trace
    from prompt import rebuild_text

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    ctx = _boot_story_plugins(workspace, shell=ScriptedSubprocess([CommandResult(0, "x")]))
    inner = FakeLLM([text_reply("好。")])
    harness_ctx = open_context_harness(
        "trace",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
        tool_registry=ctx.require(TOOLBOX_CAPABILITY),
    )
    harness_ctx.harness.send("跑")
    inner.assert_all_consumed()

    stored, _ = JsonlStore(tmp_path / "sessions").load("trace")
    assert rebuild_text(latest_prompt_trace(stored)) == effective_system_prompt(stored)
