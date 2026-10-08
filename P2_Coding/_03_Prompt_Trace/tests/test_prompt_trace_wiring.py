"""_03_Prompt_Trace 的接线测试：装配单随事件进日志 + 由日志重建（全离线）。

验收映射（本阶段 README 的验收）：
1. dump-prompt 可读、含来源信息 → `chat.show_trace` 的打印断言（见 test_prompt_trace_chat.py），
   本文件保证它拿到的是**日志里**的装配单；
2. 重建断言通过 → 核心断言：每一步模型收到的文本 == rebuild(该步日志里的装配单)；
3. 快照稳定 → 固定上下文不产生多余事件；resume 不重复记。

运行：cd P2_Coding/_03_Prompt_Trace && python -m pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from context import (
    collect_runtime_context,
    effective_system_prompt,
    latest_prompt_trace,
    open_context_harness,
)
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.session import Event, JsonlStore
from prompt import PromptAssembler, Section, default_registry, rebuild_text

TZ = timezone(timedelta(hours=8))


class StepClock:
    """步进时钟：每次采样推进 30 秒。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=30)
        return self._now


def context_at(workspace: Path, moment: datetime, platform: str = "DemoOS"):
    return collect_runtime_context(workspace, now=lambda: moment, platform_name=platform)


def system_of(messages) -> str:
    return next(m.content for m in messages if m.role == "system")


# =========================================================================
# 1) 开场装配单：进 session/start，可重建
# =========================================================================


def test_start_event_carries_trace(tmp_path):
    """新会话：session/start 携带装配单；rebuild(装配单) == system_prompt。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    open_context_harness(
        "s1",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )

    events, _ = JsonlStore(tmp_path / "sessions").load("s1")
    start = next(e for e in events if e.type == "session/start")
    trace = start.data["prompt_trace"]
    assert rebuild_text(trace) == start.data["system_prompt"]
    # 装配单含来源与引用变量信息（dump-prompt 的原料）
    names = [s["name"] for s in trace["sections"]]
    assert names == ["role", "tools", "env", "style"]
    env = next(s for s in trace["sections"] if s["name"] == "env")
    assert env["source"] == "builtin"
    assert env["referenced"] == ["cwd", "platform", "time"]


def test_latest_prompt_trace_from_disk(tmp_path):
    """从磁盘日志读回：latest_prompt_trace(事件) → 重建 == 生效文本。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    open_context_harness(
        "s2",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    events, _ = JsonlStore(tmp_path / "sessions").load("s2")
    assert rebuild_text(latest_prompt_trace(events)) == effective_system_prompt(events)


# =========================================================================
# 2) 每 step：装配单随变化进日志；模型收到的 == 由日志重建的
# =========================================================================


def test_per_step_trace_rebuilds_each_request(tmp_path):
    """脚本时钟驱动两步渲染：每一步模型收到的文本 == rebuild(该步日志装配单)。"""
    workspace = tmp_path / "ws"
    clock = StepClock(datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ))
    inner = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "6*7"}),
            text_reply("42。"),
        ]
    )
    ctx = open_context_harness(
        "s3",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: collect_runtime_context(workspace, now=clock, platform_name="DemoOS"),
    )
    ctx.harness.send("帮我算 6*7")
    inner.assert_all_consumed()

    events = ctx.session.events
    updates = [e for e in events if e.type == "system/message"]
    assert len(updates) == 2
    for update in updates:
        assert rebuild_text(update.data["prompt_trace"]) == update.data["content"]

    # ★ 核心断言：每一步实际发给模型的文本 == 由该步之前的日志重建的文本
    seen = [system_of(request.messages) for request in inner.requests]
    rebuilt = [
        rebuild_text(latest_prompt_trace(events[: index + 1]))
        for index, event in enumerate(events)
        if event.type == "assistant/message"
    ]
    assert rebuilt == seen
    assert seen[0] != seen[1]  # 两步的渲染确实不同（时间在走）


def test_trace_of_step_two_reflects_later_time(tmp_path):
    """第一步与第二步的装配单里，变量 time 的取值不同（每 step 重新采样被记录）。"""
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
    updates = [e for e in ctx.session.events if e.type == "system/message"]
    first, second = (e.data["prompt_trace"]["variables"]["time"] for e in updates)
    assert "09:01:00" in first and "09:01:30" in second


# =========================================================================
# 3) resume：不重复记；追溯链不断
# =========================================================================


def test_resume_keeps_trace_available(tmp_path):
    """resume 后（上下文未变）：不产生新事件，但装配单仍能从日志取到并重建。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    ctx1 = open_context_harness(
        "s5",
        provider=FakeLLM([text_reply("a")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    ctx1.harness.send("你好")

    ctx2 = open_context_harness(
        "s5",
        provider=FakeLLM([text_reply("b")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    ctx2.harness.send("继续")

    types = [e.type for e in ctx2.session.events]
    assert "system/message" not in types  # 渲染无变化，不重复记
    trace = latest_prompt_trace(ctx2.session.events)
    assert rebuild_text(trace) == effective_system_prompt(ctx2.session.events)


def test_provider_last_trace_available_without_record(tmp_path):
    """渲染无变化（未记录）时，provider.last_trace 仍持有最近一次的装配单。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    ctx = open_context_harness(
        "s6",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: context_at(workspace, moment),
    )
    ctx.harness.send("你好")
    trace = ctx.provider.last_trace
    assert trace is not None
    assert rebuild_text(trace.to_dict()) == trace.text


# =========================================================================
# 4) 作用域来源进装配单（与 _01 机制的叠加视角）
# =========================================================================


def test_scope_source_recorded_in_log(tmp_path):
    """作用域遮蔽/追加的 section：日志装配单里带作用域来源名。"""
    workspace = tmp_path / "ws"
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    registry = default_registry()
    scope = registry.scoped("custom")
    scope.register(Section("env", "自定义环境：{{cwd}}。"))

    open_context_harness(
        "s7",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        assembler=PromptAssembler(scope),
        context_source=lambda: context_at(workspace, moment),
    )
    events, _ = JsonlStore(tmp_path / "sessions").load("s7")
    trace = latest_prompt_trace(events)
    env = next(s for s in trace["sections"] if s["name"] == "env")
    assert env["source"] == "custom"
    assert rebuild_text(trace) == effective_system_prompt(events)


# =========================================================================
# 5) 没有装配单的会话：追溯不到，但文本照取（诚实降级）
# =========================================================================


def test_trace_absent_is_none_not_error():
    """session/start 没带装配单（如 _02 时代的老日志）→ None，不抛错。"""
    events = [
        Event(seq=1, type="session/start", data={"session_id": "old", "system_prompt": "T0"}),
        Event(seq=2, type="user/message", data={"content": "hi"}),
    ]
    assert latest_prompt_trace(events) is None
    assert effective_system_prompt(events) == "T0"  # 文本本身仍可取到
