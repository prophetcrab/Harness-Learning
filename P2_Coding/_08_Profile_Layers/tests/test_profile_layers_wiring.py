"""_08_Profile_Layers 的接线测试：配置树 → 插件 → ctx → 完整会话（全离线）。

验收映射（本阶段 README）：
- 「同 base、两 profile 产出不同且可读的树」→ dev/prod 对照 + boot 出的 ctx 槽位/类型；
- 「换 LLM provider 只改一行」→ 一行 PatchRow 换 llm 行的 name，boot 后 ctx 里换人；
- 「叠加顺序正确」→ base→profile→user→CLI 的端到端效果（CLI 覆盖 profile）；
- 配置错误/激活失败：fail loud，且 ctx 不残留半装状态。

运行：cd P2_Coding/_08_Profile_Layers && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ConfigError, Patch, PatchRow, apply_patch, load_profile
from context import collect_runtime_context, open_context_harness
from harness.session import JsonlStore
from harness.tools import AutoApprove
from kernel import DuplicateSlotError
from providers import (
    LLM_CAPABILITY,
    TOOLBOX_CAPABILITY,
    boot_tree,
    build_plugins_from_tree,
)

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
STAGE_ROOT = Path(__file__).resolve().parents[1]


# =========================================================================
# 1) 两个 profile 激活成不同的 ctx
# =========================================================================


def test_dev_profile_boots_offline_stack(tmp_path: Path):
    tree = load_profile("dev", profiles_dir=PROFILES)
    ctx = boot_tree(tree, stage_root=STAGE_ROOT)

    assert ctx.slots == ["llm", "fs", "subprocess", "toolbox"]
    assert type(ctx.require(LLM_CAPABILITY)).__name__ == "FakeLLM"
    assert type(ctx.get("fs")).__name__ == "MemoryFS"
    assert type(ctx.get("subprocess")).__name__ == "ScriptedSubprocess"
    registry = ctx.require(TOOLBOX_CAPABILITY)
    assert registry.names == ["calculate", "read_file", "write_file", "list_files", "shell"]


def test_prod_profile_would_use_real_implementations():
    """prod 的**配置**指向真实实现（不 boot——boot 会真去构造 DeepSeek/LocalFS）。

    这里只验证工厂注册表认得这些 name、且构造出的插件名正确。
    """
    tree = load_profile("prod", profiles_dir=PROFILES)
    # 用假替身避免真 key：直接检查 name → 工厂存在
    from providers import PLUGIN_FACTORIES

    for row in tree.active_rows():
        assert row.name in PLUGIN_FACTORIES
    assert tree.require("llm").name == "llm:deepseek"
    assert tree.require("fs").name == "fs:local"
    assert tree.require("subprocess").name == "subprocess:local"


def test_one_line_patch_switches_llm_row(tmp_path: Path):
    """一行 patch 换 provider：dev 骨架只动 llm 行 → boot 后 ctx 里换了人。"""
    cli = tmp_path / "cli.yaml"
    cli.write_text("patch:\n  - id: llm\n    name: llm:fake\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    ctx = boot_tree(tree, stage_root=STAGE_ROOT)
    assert type(ctx.require(LLM_CAPABILITY)).__name__ == "FakeLLM"
    # 其余行不受影响
    assert type(ctx.get("fs")).__name__ == "MemoryFS"


def test_disable_row_removes_it_from_boot(tmp_path: Path):
    cli = tmp_path / "cli.yaml"
    cli.write_text("patch:\n  - id: subprocess\n    disabled: true\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    ctx = boot_tree(tree, stage_root=STAGE_ROOT)
    assert "subprocess" not in ctx.slots
    assert "shell" not in ctx.require(TOOLBOX_CAPABILITY).names  # 工具面跟着少一个


def test_insert_new_row_activates_it(tmp_path: Path):
    """insert 追加新行：出现在树的末尾、被激活。"""
    cli = tmp_path / "cli.yaml"
    cli.write_text(
        "insert:\n  - id: fs-extra\n    name: fs:memory\n", encoding="utf-8"
    )
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    assert tree.ids[-1] == "fs-extra"
    # 第二份 fs 会撞槽位——正好验证 boot 的 fail loud 与回卷
    with pytest.raises(DuplicateSlotError):
        boot_tree(tree, stage_root=STAGE_ROOT)


# =========================================================================
# 2) 激活失败：fail loud + 不留半装状态
# =========================================================================


def test_boot_failure_leaves_no_partial_state(tmp_path: Path):
    """激活中途失败（槽位冲突）：ctx 是新建的、失败后即弃——不残留半装台账。"""
    cli = tmp_path / "cli.yaml"
    cli.write_text("insert:\n  - id: fs-extra\n    name: fs:memory\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    with pytest.raises(DuplicateSlotError):
        boot_tree(tree, stage_root=STAGE_ROOT)
    # 重新 boot 一份干净配置照常工作（没有全局污染）
    ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=STAGE_ROOT)
    assert ctx.slots == ["llm", "fs", "subprocess", "toolbox"]


# =========================================================================
# 3) 配置驱动的完整会话
# =========================================================================


def test_session_on_dev_profile_end_to_end(tmp_path: Path):
    """dev profile：boot → 会话（算数 + 命令 + 写文件）→ 日志可重放。"""
    tree = load_profile("dev", profiles_dir=PROFILES)
    ctx = boot_tree(tree, stage_root=STAGE_ROOT)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    harness_ctx = open_context_harness(
        "profile-e2e",
        provider=ctx.require(LLM_CAPABILITY),
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
        tool_registry=ctx.require(TOOLBOX_CAPABILITY),
    )
    first = harness_ctx.harness.send("帮我算 1234*56.78")
    second = harness_ctx.harness.send("用 shell 看一眼目录")

    assert first.steps[0].tool_results[0].result["result"] == pytest.approx(70066.52)
    assert second.steps[0].tool_results[0].result["exit_code"] == 0  # 剧本回放
    # dev 的 fs 是 MemoryFS：工具写入不进磁盘
    assert list(workspace.iterdir()) == []

    events, _ = JsonlStore(tmp_path / "sessions").load("profile-e2e")
    assert any(e.type == "tool/result" and e.data["name"] == "shell" for e in events)


def test_two_profiles_same_script_different_backends(tmp_path: Path):
    """同一段操作、两份配置：dev（内存）与"dev 但 fs 换成 local"（磁盘）结果相同、副作用不同。"""
    script_questions = ["把要点写到 notes/a.txt"]

    def run(tree, session_id: str):
        ctx = boot_tree(tree, stage_root=STAGE_ROOT)
        workspace = tmp_path / f"ws-{session_id}"
        workspace.mkdir(parents=True, exist_ok=True)
        # 为两场会话各造一份剧本
        from harness.llm import FakeLLM, text_reply, tool_call_reply

        provider = FakeLLM(
            [
                tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hi"}),
                text_reply("写好了。"),
            ]
        )
        harness_ctx = open_context_harness(
            session_id,
            provider=provider,
            root=tmp_path / "sessions",
            workspace=workspace,
            approval=AutoApprove(),
            context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
            tool_registry=ctx.require(TOOLBOX_CAPABILITY),
        )
        result = harness_ctx.harness.send(script_questions[0])
        provider.assert_all_consumed()
        return result, workspace

    dev_tree = load_profile("dev", profiles_dir=PROFILES)
    local_tree = apply_patch(
        dev_tree,
        Patch(replacements=[PatchRow(id="fs", name="fs:local", config={"root": "demo_workspace/ws"})]),
    )

    dev_result, dev_ws = run(dev_tree, "dev-side")
    local_result, local_ws = run(local_tree, "local-side")

    # 工具结果一致（都成功写了 notes/a.txt）
    assert dev_result.steps[0].tool_results[0].result["written"] == "notes/a.txt"
    assert local_result.steps[0].tool_results[0].result["written"] == "notes/a.txt"
    # 副作用不同：dev 磁盘无痕迹；local 真落盘
    assert list(dev_ws.iterdir()) == []
    assert (STAGE_ROOT / "demo_workspace" / "ws" / "notes" / "a.txt").is_file()
    (STAGE_ROOT / "demo_workspace" / "ws" / "notes" / "a.txt").unlink()  # 清理


# =========================================================================
# 4) 配置错误在入口前被拦
# =========================================================================


def test_config_error_before_any_side_effect(tmp_path: Path):
    """未知插件名：构造阶段就拦，不产生任何对象、不碰任何槽位。"""
    tree = apply_patch(
        load_profile("dev", profiles_dir=PROFILES),
        Patch(replacements=[PatchRow(id="llm", name="llm:ghost")]),
    )
    with pytest.raises(ConfigError):
        build_plugins_from_tree(tree, stage_root=STAGE_ROOT)
