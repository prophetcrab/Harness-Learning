"""_04_Filesystem_Seam 的接线测试：接缝工具跑完整会话（全离线，FakeLLM 驱动）。

验收映射（本阶段 README）：
- 「同一套工具测试在 local 与 memory 两个 provider 下都全绿」→ 本文件用一个共享的
  故事函数在两份 provider 上各跑一遍，断言工具结果**逐字段相同**；
- 「provider 在显式 resolve 处选定」→ 会话由 ServiceContainer.resolve 的产物驱动；
- 「基线 22 用例仍全绿」→ test_filesystem_seam_assembly.py（未改）。

运行：cd P2_Coding/_04_Filesystem_Seam && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

from context import collect_runtime_context, open_context_harness
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.session import JsonlStore
from harness.tools import ScriptedApprover
from harness.tools.approval import ApprovalDecision
from providers import (
    FS_CAPABILITY,
    LocalFS,
    MemoryFS,
    ServiceContainer,
    build_filesystem_registry,
)

NOTE = "接缝笔记\n- 工具只认协议\n- provider 决定数据去哪"


def _story() -> FakeLLM:
    """一段固定剧本：写 → 读 → 列（三个 turn）。"""
    return FakeLLM(
        [
            tool_call_reply("write_file", {"path": "notes/seam.txt", "content": NOTE}),
            text_reply("已写入。"),
            tool_call_reply("read_file", {"path": "notes/seam.txt"}),
            text_reply("读回来了。"),
            tool_call_reply("list_files", {"path": "."}),
            text_reply("看到了。"),
        ]
    )


def _run_story(fs, tmp_path: Path, session_id: str):
    """用一段固定剧本驱动一场会话，返回 (结果三元组, harness)。"""
    inner = _story()
    ctx = open_context_harness(
        session_id,
        provider=inner,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=ScriptedApprover([ApprovalDecision(True, "批准")]),
        context_source=lambda: collect_runtime_context(
            tmp_path / "ws", platform_name="DemoOS"
        ),
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


# =========================================================================
# 1) ★ 核心验收：同一段会话，两个 provider，工具结果逐字段相同
# =========================================================================


def test_same_conversation_two_providers(tmp_path: Path):
    local_results, _ = _run_story(LocalFS(tmp_path / "local_ws"), tmp_path, "s-local")
    memory_results, _ = _run_story(MemoryFS(), tmp_path, "s-memory")

    for local, memory in zip(local_results, memory_results, strict=True):
        assert _tool_results(local) == _tool_results(memory)
    # 读回的内容也确实一致（不是空手相等）
    assert memory_results[1].steps[0].tool_results[0].result == {
        "path": "notes/seam.txt",
        "content": NOTE,
    }


def test_local_provider_persists_to_disk(tmp_path: Path):
    """LocalFS：会话结束后文件真的在磁盘上。"""
    local_ws = tmp_path / "local_ws"
    _run_story(LocalFS(local_ws), tmp_path, "persist")
    assert (local_ws / "notes" / "seam.txt").read_text(encoding="utf-8") == NOTE


def test_memory_provider_leaves_no_trace(tmp_path: Path):
    """MemoryFS：整场会话跑完，哨兵工作区目录不存在（磁盘零痕迹）。"""
    sentinel = tmp_path / "memory_ws"
    _run_story(MemoryFS(), tmp_path, "clean")
    assert not sentinel.exists()
    assert not (tmp_path / "ws").exists()  # open_context_harness 传的 workspace 也没被碰


def test_resume_with_memory_provider_loses_files_but_not_history(tmp_path: Path):
    """MemoryFS 的"本性"：进程结束（provider 换新）后文件不在；但历史在日志里。"""
    fs1 = MemoryFS()
    _run_story(fs1, tmp_path, "memory-resume")

    # 模拟"重开进程"：新的 MemoryFS（内存已清空）
    inner = FakeLLM(
        [
            tool_call_reply("list_files", {"path": "."}),
            text_reply("空的。"),
        ]
    )
    ctx = open_context_harness(
        "memory-resume",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=ScriptedApprover([]),
        context_source=lambda: collect_runtime_context(tmp_path / "ws", platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(MemoryFS()),
    )
    result = ctx.harness.send("还有什么文件？")
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].result["files"] == []  # 文件没了
    roles = [m.role for m in ctx.harness.messages]
    assert "tool" in roles  # 但之前的历史还在（日志投影）
    assert len(roles) > 4


# =========================================================================
# 2) 显式 resolve：会话由容器解析出的 provider 驱动（铁律 #6）
# =========================================================================


def test_session_uses_resolved_provider(tmp_path: Path):
    """用 ServiceContainer 显式 resolve 一个 provider 再开会话——工具跟着它走。"""
    services = ServiceContainer()
    services.register(FS_CAPABILITY, MemoryFS())
    fs = services.resolve(FS_CAPABILITY)

    results, _ = _run_story(fs, tmp_path, "resolved")
    assert results[0].status == "done"
    assert fs.read_text("notes/seam.txt") == NOTE  # 数据确实在这份 provider 里


# =========================================================================
# 3) 错误路径也是"给模型的输入"（铁律 #7）
# =========================================================================


def test_missing_file_error_reaches_model_as_tool_result(tmp_path: Path):
    """读一个不存在的文件：工具不炸循环，模型拿到结构化错误。"""
    inner = FakeLLM(
        [
            tool_call_reply("read_file", {"path": "nope.txt"}),
            text_reply("文件不存在，我知道了。"),
        ]
    )
    ctx = open_context_harness(
        "errors",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        context_source=lambda: collect_runtime_context(tmp_path / "ws", platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(MemoryFS()),
    )
    result = ctx.harness.send("读一个不存在的文件")
    inner.assert_all_consumed()

    assert result.status == "done"
    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is True
    assert tool_result.result["code"] == "FS_NOT_FOUND"
    # 错误也落了日志（模型可见 ⟺ 已记录）
    events, _ = JsonlStore(tmp_path / "sessions").load("errors")
    assert any(e.type == "tool/result" and e.data["is_error"] for e in events)


def test_approval_still_applies_to_seam_tools(tmp_path: Path):
    """接缝工具保留了审批标记：拒绝后文件不落盘（write_file needs_approval 未丢）。"""
    from harness.tools.approval import AutoDeny

    local_ws = tmp_path / "ws"
    inner = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "a.txt", "content": "x"}),
            text_reply("好，不写。"),
        ]
    )
    ctx = open_context_harness(
        "deny",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=local_ws,
        approval=AutoDeny("测试拒绝"),
        context_source=lambda: collect_runtime_context(local_ws, platform_name="DemoOS"),
        tool_registry=build_filesystem_registry(LocalFS(local_ws)),
    )
    result = ctx.harness.send("写个文件")
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].error is True
    assert not (local_ws / "a.txt").exists()


# =========================================================================
# 4) 与提示词机制共存：接缝工具下装配单照常
# =========================================================================


def test_prompt_trace_still_works_with_seam_tools(tmp_path: Path):
    """工具面换成接缝注册表后，提示词侧机制（装配单/重建）不受影响。"""
    from context import effective_system_prompt, latest_prompt_trace
    from prompt import rebuild_text

    _, harness = _run_story(MemoryFS(), tmp_path, "trace-check")
    stored_events, _ = JsonlStore(tmp_path / "sessions").load("trace-check")
    assert rebuild_text(latest_prompt_trace(stored_events)) == effective_system_prompt(
        stored_events
    )
