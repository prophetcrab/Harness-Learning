"""_05_Workspace_Jail 的接线测试：围栏跑完整会话（全离线，FakeLLM 驱动）。

验收映射（本阶段 README）：
- 「jail 越权返回的错误结构与其它工具错误一致」→ 工具层断言三种错误同构；
- 「审批放行也拦（沙箱是第二道防线）」→ 审批批准后越界写依然被拒、磁盘无痕迹；
- 「local / jail / memory 三种实现可切换」→ 同一段对话在三种 provider 上跑，
  区内操作结果逐字段相同；越界操作只有 jail 拒绝。

运行：cd P2_Coding/_05_Workspace_Jail && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

from context import collect_runtime_context, open_context_harness
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.session import JsonlStore
from harness.tools import AutoApprove, ScriptedApprover
from harness.tools.approval import ApprovalDecision
from providers import (
    FS_CAPABILITY,
    FS_SANDBOX_DENIED,
    LocalFS,
    MemoryFS,
    ServiceContainer,
    WorkspaceJailFS,
    build_filesystem_registry,
)

NOTE = "围栏笔记\n- 改动限定在工作区内\n- 审批放行也拦"


def _story() -> FakeLLM:
    """一段固定剧本：写 → 读 → 列（三个 turn，全部区内）。"""
    return FakeLLM(
        [
            tool_call_reply("write_file", {"path": "notes/jail.txt", "content": NOTE}),
            text_reply("已写入。"),
            tool_call_reply("read_file", {"path": "notes/jail.txt"}),
            text_reply("读回来了。"),
            tool_call_reply("list_files", {"path": "."}),
            text_reply("看到了。"),
        ]
    )


def _run_story(fs, tmp_path: Path, session_id: str):
    inner = _story()
    ctx = open_context_harness(
        session_id,
        provider=inner,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=ScriptedApprover([ApprovalDecision(True, "批准")]),
        context_source=lambda: collect_runtime_context(tmp_path / "ws", platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(fs),
    )
    results = [
        ctx.harness.send("写个文件"),
        ctx.harness.send("读回来"),
        ctx.harness.send("列一下"),
    ]
    inner.assert_all_consumed()
    return results, ctx.harness


def _tool_results(result):
    return [
        (step.index, tuple((r.name, r.result, r.error) for r in step.tool_results))
        for step in result.steps
    ]


def _with_jail(tmp_path: Path, name: str):
    return WorkspaceJailFS(LocalFS(tmp_path / name))


# =========================================================================
# 1) 三种实现可切换：区内操作结果逐字段相同
# =========================================================================


def test_three_providers_same_inside_results(tmp_path: Path):
    local_results, _ = _run_story(LocalFS(tmp_path / "local_ws"), tmp_path, "s-local")
    jail_results, _ = _run_story(_with_jail(tmp_path, "jail_ws"), tmp_path, "s-jail")
    memory_results, _ = _run_story(MemoryFS(), tmp_path, "s-memory")

    base = [_tool_results(r) for r in local_results]
    assert base == [_tool_results(r) for r in jail_results]
    assert base == [_tool_results(r) for r in memory_results]


def test_provider_switch_via_service_container(tmp_path: Path):
    """通过单槽服务显式 resolve 三种 provider：每次都是"解析点决定用哪个"。"""
    for provider_factory, expected in (
        (lambda: LocalFS(tmp_path / "a"), "LocalFS"),
        (lambda: WorkspaceJailFS(LocalFS(tmp_path / "b")), "WorkspaceJailFS"),
        (lambda: MemoryFS(), "MemoryFS"),
    ):
        services = ServiceContainer()
        services.register(FS_CAPABILITY, provider_factory())  # 单槽：每次新容器
        fs = services.resolve(FS_CAPABILITY)
        assert type(fs).__name__ == expected
        results, _ = _run_story(fs, tmp_path, f"switch-{expected}")
        assert results[0].status == "done"


# =========================================================================
# 2) ★ 审批放行也拦：沙箱是第二道防线
# =========================================================================


def test_jail_blocks_escape_even_when_approved(tmp_path: Path):
    """审批批准了越界写（AutoApprove = 无条件放行），沙箱仍然拒绝。"""
    jail_ws = tmp_path / "jail_ws"
    inner = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "../escape.txt", "content": "越狱尝试"}),
            text_reply("被拒绝了，我换地方。"),
        ]
    )
    ctx = open_context_harness(
        "escape",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=jail_ws,
        approval=AutoApprove(),  # ★ 审批无条件放行——但沙箱是第二道防线
        context_source=lambda: collect_runtime_context(jail_ws, platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(WorkspaceJailFS(LocalFS(jail_ws))),
    )
    result = ctx.harness.send("把这行字写到 ../escape.txt")
    inner.assert_all_consumed()

    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is True
    assert tool_result.result["code"] == FS_SANDBOX_DENIED
    assert not (tmp_path / "escape.txt").exists()  # ★ 磁盘无痕迹

    # 拒绝也落了日志（模型可见 ⟺ 已记录）
    events, _ = JsonlStore(tmp_path / "sessions").load("escape")
    assert any(e.type == "tool/result" and e.data["is_error"] for e in events)


def test_local_allows_escape_when_approved(tmp_path: Path):
    """对照组：没有围栏的 local provider——同样的操作直接放行（真的写到区外）。"""
    local_ws = tmp_path / "local_ws"
    inner = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "../escaped.txt", "content": "出去了"}),
            text_reply("写好了。"),
        ]
    )
    ctx = open_context_harness(
        "no-fence",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=local_ws,
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(local_ws, platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(LocalFS(local_ws)),
    )
    result = ctx.harness.send("写")
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].error is False
    assert (tmp_path / "escaped.txt").read_text(encoding="utf-8") == "出去了"
    # → 这一对用例就是"策略 provider 改变了产品行为"的最小证据


# =========================================================================
# 3) 工具层错误同构：越权与其它文件错误一个形状
# =========================================================================


def test_tool_errors_are_isomorphic(tmp_path: Path):
    registry = build_filesystem_registry(WorkspaceJailFS(LocalFS(tmp_path / "ws")))
    read = registry.resolve("read_file").func
    write = registry.resolve("write_file").func

    missing = read(path="none.txt")
    denied = write(path="../x.txt", content="x")

    assert set(missing) == set(denied) == {"error", "code"}
    assert missing["code"] == "FS_NOT_FOUND"
    assert denied["code"] == FS_SANDBOX_DENIED
    assert isinstance(missing["error"], str) and isinstance(denied["error"], str)


def test_denied_write_result_reaches_model_and_model_recovers(tmp_path: Path):
    """模型拿到结构化拒绝后换个区内路径重试（剧本模拟），第二次成功。"""
    jail_ws = tmp_path / "ws"
    inner = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "../evil.txt", "content": "x"}, call_id="c1"),
            tool_call_reply("write_file", {"path": "safe/ok.txt", "content": "x"}, call_id="c2"),
            text_reply("换了位置，写好了。"),
        ]
    )
    ctx = open_context_harness(
        "recover",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=jail_ws,
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(jail_ws, platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(WorkspaceJailFS(LocalFS(jail_ws))),
    )
    result = ctx.harness.send("写两次")
    inner.assert_all_consumed()

    assert result.status == "done"
    first = result.steps[0].tool_results[0]
    second = result.steps[1].tool_results[0]
    assert first.error is True and first.result["code"] == FS_SANDBOX_DENIED
    assert second.error is False
    assert (jail_ws / "safe" / "ok.txt").is_file()


# =========================================================================
# 4) 围栏不干扰其它机制：历史/提示词/日志照常
# =========================================================================


def test_prompt_trace_and_history_unaffected(tmp_path: Path):
    from context import effective_system_prompt, latest_prompt_trace, project
    from prompt import rebuild_text

    _, harness = _run_story(_with_jail(tmp_path, "trace_ws"), tmp_path, "trace-check")
    stored, _ = JsonlStore(tmp_path / "sessions").load("trace-check")
    assert rebuild_text(latest_prompt_trace(stored)) == effective_system_prompt(stored)
    roles = [m.role for m in project(stored)]
    assert roles[0] == "system" and "tool" in roles
