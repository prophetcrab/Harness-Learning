"""_02_Prompt_Context 的端到端接线测试：open_context_harness（全离线，FakeLLM 驱动）。

验收映射（本阶段 README 的四条验收）：
1. 三种变量插值正确 → 新会话的 session/start 里就是渲染后的文本（含 cwd/platform/time）；
2. 改 cwd 只影响对应 section → unit 测试逐节对照（test_prompt_context_unit.py）；
   本文件补一条"作用域遮蔽后整体渲染正确"的接线断言；
3. system/message 进日志且可投影 → 时钟推进时每步渲染变化都落一条事件，投影只留最新；
4. 快照稳定 → 固定上下文下渲染无变化，日志里不产生多余的 system/message。

核心断言（铁律 #1 在本阶段的形状）：
    模型每一步实际收到的 system 文本 == 由日志（含 system/message 遮蔽）重建的文本。

运行：cd P2_Coding/_02_Prompt_Context && python -m pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context import (
    RuntimeContext,
    collect_runtime_context,
    effective_system_prompt,
    open_context_harness,
    project,
)
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.llm.vocabulary import Message
from harness.session import JsonlStore
from harness.tools import AutoApprove
from prompt import PromptAssembler, Section, default_registry

TZ = timezone(timedelta(hours=8))


class StepClock:
    """步进时钟：每次采样推进 30 秒。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=30)
        return self._now


def context_at(workspace: Path, moment: datetime, platform: str = "DemoOS") -> RuntimeContext:
    return collect_runtime_context(workspace, now=lambda: moment, platform_name=platform)


def system_of(messages: list[Message]) -> str:
    """请求里的 system 文本（断言用）。"""
    return next(m.content for m in messages if m.role == "system")


# =========================================================================
# 1) 新会话：开场渲染进 session/start
# =========================================================================


def test_open_new_session_renders_baseline(tmp_path):
    """新会话把"开场那一刻的渲染"写进 session/start（含三个变量的值）。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    inner = FakeLLM([text_reply("hi")])

    open_context_harness(
        "s1",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )

    events, _ = JsonlStore(tmp_path / "sessions").load("s1")
    start = next(e for e in events if e.type == "session/start")
    assert "DemoOS" in start.data["system_prompt"]
    assert "2026-10-08T09:00:00+08:00" in start.data["system_prompt"]
    assert str(workspace.resolve()) in start.data["system_prompt"]
    assert effective_system_prompt(events) == start.data["system_prompt"]


# =========================================================================
# 2) 每 step 渲染：变化才记录；模型收到的 == 日志重建的
# =========================================================================


def test_per_step_rendering_logs_and_rebuilds(tmp_path):
    """时钟推进（每采样 +30s）：每步渲染不同 → 逐条进日志；请求文本可由日志前缀重建。"""
    workspace = tmp_path / "ws"
    clock = StepClock(datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ))
    inner = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "6*7"}),
            text_reply("6 × 7 = 42。"),
        ]
    )
    ctx = open_context_harness(
        "s2",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: collect_runtime_context(workspace, now=clock, platform_name="DemoOS"),
    )
    result = ctx.harness.send("帮我算 6*7")
    assert result.status == "done"
    inner.assert_all_consumed()

    events = ctx.session.events
    updates = [e for e in events if e.type == "system/message"]
    assert len(updates) == 2  # 两步，两次变化
    assert "09:01:00" in updates[0].data["content"]
    assert "09:01:30" in updates[1].data["content"]

    # ★ 核心断言：每一步模型实际收到的文本 == 由日志前缀重建的文本
    seen = [system_of(request.messages) for request in inner.requests]
    rebuilt = [
        effective_system_prompt(events[: index + 1])
        for index, event in enumerate(events)
        if event.type == "assistant/message"
    ]
    assert rebuilt == seen
    assert seen[0] != seen[1]  # 两步的渲染确实不同（时间在走）


def test_fixed_context_does_not_log_duplicates(tmp_path):
    """固定上下文（快照稳定）：渲染永远一样 → 一个 system/message 都不产生。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    inner = FakeLLM([text_reply("hi")])
    ctx = open_context_harness(
        "s3",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    ctx.harness.send("你好")
    inner.assert_all_consumed()

    types = [e.type for e in ctx.session.events]
    assert "system/message" not in types
    assert system_of(inner.requests[0].messages) == effective_system_prompt(ctx.session.events)


def test_projection_has_single_latest_system(tmp_path):
    """投影视图：只有一条 system 消息，且是最近一次渲染。"""
    workspace = tmp_path / "ws"
    clock = StepClock(datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ))
    inner = FakeLLM([tool_call_reply("calculate", {"expression": "1+1"}), text_reply("2")])
    ctx = open_context_harness(
        "s4",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: collect_runtime_context(workspace, now=clock, platform_name="DemoOS"),
    )
    ctx.harness.send("算 1+1")

    messages = project(ctx.session.events)
    systems = [m for m in messages if m.role == "system"]
    assert len(systems) == 1
    assert systems[0].content == effective_system_prompt(ctx.session.events)
    assert "09:01:30" in systems[0].content  # 最近一次（第 2 步）的渲染


# =========================================================================
# 3) resume：以日志为基线，不重复记录；上下文变了才追加
# =========================================================================


def test_resume_same_context_adds_no_duplicate(tmp_path):
    """重开时上下文与上次一致 → 渲染 == 日志基线 → 不产生新事件。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    first = FakeLLM([text_reply("a")])
    ctx1 = open_context_harness(
        "s5",
        provider=first,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    ctx1.harness.send("你好")

    second = FakeLLM([text_reply("b")])
    ctx2 = open_context_harness(
        "s5",
        provider=second,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    ctx2.harness.send("继续")
    second.assert_all_consumed()

    assert [e.type for e in ctx2.session.events].count("system/message") == 0
    assert ctx2.harness.session.last_turn_number == 2


def test_resume_with_changed_context_appends_update(tmp_path):
    """重开时上下文变了（时间前进）→ 首步渲染即变化，追加一条 system/message。"""
    workspace = tmp_path / "ws"
    t1 = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    t2 = datetime(2026, 10, 8, 10, 0, 0, tzinfo=TZ)

    first = FakeLLM([text_reply("a")])
    ctx1 = open_context_harness(
        "s6",
        provider=first,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, t1),
    )
    ctx1.harness.send("你好")

    second = FakeLLM([text_reply("b")])
    ctx2 = open_context_harness(
        "s6",
        provider=second,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, t2),
    )
    ctx2.harness.send("现在几点？")
    second.assert_all_consumed()

    updates = [e for e in ctx2.session.events if e.type == "system/message"]
    assert len(updates) == 1
    assert "10:00:00" in updates[0].data["content"]
    # 模型第 2 个 turn 看到的是更新后的渲染
    assert "10:00:00" in system_of(second.requests[0].messages)
    # 而历史里更早的消息不受影响（日志只增不改）
    types = [e.type for e in ctx2.session.events]
    assert types[:2] == ["session/start", "turn/start"]


# =========================================================================
# 4) 与 _01 机制的叠加：作用域遮蔽走完整接线
# =========================================================================


def test_scope_override_flows_to_provider(tmp_path):
    """作用域遮蔽 env 一节 → 模型收到的 system 是遮蔽后的版本。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    registry = default_registry()
    scope = registry.scoped("custom")
    scope.register(Section("env", "自定义环境说明：目录 {{cwd}}，时间 {{time}}。"))

    inner = FakeLLM([text_reply("hi")])
    ctx = open_context_harness(
        "s7",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        assembler=PromptAssembler(scope),
        context_source=lambda: context_at(workspace, moment),
    )
    ctx.harness.send("你好")
    inner.assert_all_consumed()

    assert "自定义环境说明" in system_of(inner.requests[0].messages)
    assert "运行时环境" not in system_of(inner.requests[0].messages)


def test_unknown_variable_fails_loud_at_open(tmp_path):
    """装配器引用未提供的变量 → 开会话（渲染基线）时就 fail loud，不让半个模板进日志。"""
    registry = default_registry()
    registry.unregister("env")
    registry.register(Section("env", "引用了 {{ghost}}。"))
    with pytest.raises(ValueError, match="未知变量：ghost"):
        open_context_harness(
            "s8",
            provider=FakeLLM([]),
            root=tmp_path / "sessions",
            workspace=tmp_path / "ws",
            assembler=PromptAssembler(registry),
        )


# =========================================================================
# 5) 审批/工具路径不受影响（基线行为回归的补充视角）
# =========================================================================


def test_tool_path_still_works_with_wrapper(tmp_path):
    """write_file 经包装后的 provider 仍走审批与落盘（包装是透明的）。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    inner = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "a.txt", "content": "hello"}),
            text_reply("已写入。"),
        ]
    )
    ctx = open_context_harness(
        "s9",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
        context_source=lambda: context_at(workspace, moment),
    )
    result = ctx.harness.send("写个文件")
    inner.assert_all_consumed()

    assert result.status == "done"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "hello"
    assert result.steps[0].tool_results[0].error is False
