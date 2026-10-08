"""_02_Prompt_Context 的单元测试：插值 / 渲染器 / 运行时上下文 / 会话扩展 / provider 包装。

与同目录另两个基线文件（`test_prompt_context_assembly.py` / `_webui.py`，22 用例）的分工：
本文件只测试 **本阶段新增的机制**，以及各部件之间的契约。

覆盖：
1. 插值 render_text：基本替换 / 空白容错 / 未知变量 fail loud / 非法名 fail loud /
   非字符串值 fail loud / referenced_names 按序
2. 渲染器 PromptRenderer：每次调用重新采样（时间会走）；改 cwd 只影响引用它的 section
3. RuntimeContext：不可变快照；collect_runtime_context 可注入 now/platform
4. ContextSession：system/message 可追加（其余类型仍走基线校验）；投影遮蔽（最近一次生效）
5. 投影：effective_system_prompt 的取值链；project 恒只有一条 system 消息
6. RuntimePromptProvider：替换/插入 system 消息、只在渲染变化时记录、透传 tools/model

运行：cd P2_Coding/_02_Prompt_Context && python -m pytest -q
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from context import (
    ContextSession,
    RuntimeContext,
    RuntimePromptProvider,
    effective_system_prompt,
    project,
)
from context.provider import with_system_prompt
from context.renderer import PromptRenderer
from context.runtime import collect_runtime_context
from harness.llm import FakeLLM, text_reply, user
from harness.llm.provider import LLMRequest
from harness.llm.vocabulary import system
from prompt import PromptAssembler, Section, SectionRegistry, referenced_names, render_text

TZ = timezone(timedelta(hours=8))


def _reg(*sections: Section) -> SectionRegistry:
    registry = SectionRegistry()
    for section in sections:
        registry.register(section)
    return registry


class FixedClock:
    """固定时钟：每次采样返回值不变（确定性）。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class StepClock:
    """步进时钟：每次采样推进 30 秒（模拟"时间在走"）。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=30)
        return self._now


# =========================================================================
# 1) 插值：render_text
# =========================================================================


def test_render_text_basic_replacement():
    assert render_text("a {{x}} b", {"x": "1"}) == "a 1 b"
    assert render_text("{{a}}-{{b}}", {"a": "1", "b": "2"}) == "1-2"
    assert render_text("没有占位符", {"x": "1"}) == "没有占位符"


def test_render_text_tolerates_whitespace():
    """{{ x }} 与 {{x}} 等价（两侧空白被剥掉）。"""
    assert render_text("{{ x }}", {"x": "v"}) == "v"


def test_render_text_unknown_variable_is_loud():
    """引用未提供的变量 → 报错，并列出本次提供了哪些。"""
    with pytest.raises(ValueError, match="未知变量：missing"):
        render_text("{{missing}}", {"cwd": "/w"})


def test_render_text_invalid_name_is_loud():
    """非法变量名（表达式、连字符等）→ 报错。"""
    with pytest.raises(ValueError, match="非法变量名"):
        render_text("{{cwd-path}}", {"cwd-path": "x"})


def test_render_text_non_string_value_is_loud():
    """值必须是 str —— 上下文负责先把 Path/datetime 格式化。"""
    with pytest.raises(ValueError, match="必须是字符串"):
        render_text("{{x}}", {"x": 42})  # type: ignore[dict-item]


def test_referenced_names_in_order():
    assert referenced_names("{{a}} 和 {{ b }} 和 {{a}}") == ["a", "b", "a"]
    assert referenced_names("无占位符") == []


# =========================================================================
# 2) 渲染器：每调用一次 = 重新采样一次
# =========================================================================


def test_renderer_resamples_each_call():
    """每次 render() 都向上下文来源要一份"当下"快照——时间是活的。"""
    clock = StepClock(datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ))
    assembler = PromptAssembler(_reg(Section("env", "时间 {{time}}")))
    renderer = PromptRenderer(
        assembler, lambda: collect_runtime_context("ws", now=clock, platform_name="DemoOS")
    )
    first = renderer.render()
    second = renderer.render()
    assert "09:00:30" in first and "09:01:00" in second and first != second


def test_change_cwd_only_affects_referencing_section():
    """验收：改 cwd 只影响引用 {{cwd}} 的 section，其它节逐字节不变。"""
    sections = [
        Section("role", "你是助手。"),
        Section("env", "工作目录：{{cwd}}。"),
        Section("style", "简洁。"),
    ]
    assembler = PromptAssembler(_reg(*sections))

    def by_name(variables):
        return {
            s.name: render_text(s.content, variables) for s in assembler.parts()
        }

    before = by_name({"cwd": "D:/a"})
    after = by_name({"cwd": "D:/b"})

    changed = [name for name in before if before[name] != after[name]]
    assert changed == ["env"]
    assert before["role"] == after["role"] and before["style"] == after["style"]


# =========================================================================
# 3) RuntimeContext：不可变快照 + 可注入采集
# =========================================================================


def test_runtime_context_is_frozen_snapshot():
    snap = RuntimeContext(cwd="/w", platform="DemoOS", time="T")
    with pytest.raises(FrozenInstanceError):
        snap.cwd = "/other"  # type: ignore[misc]
    assert snap.variables() == {"cwd": "/w", "platform": "DemoOS", "time": "T"}


def test_collect_runtime_context_is_injectable():
    """now / platform_name 可注入：测试与 demo 都能得到确定性输出。"""
    moment = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)
    snap = collect_runtime_context("ws", now=FixedClock(moment), platform_name="DemoOS")
    assert snap.platform == "DemoOS"
    assert snap.time == "2026-10-08T09:00:00+08:00"
    assert snap.cwd.endswith("ws")


# =========================================================================
# 4) ContextSession：扩展事件词汇 + 遮蔽投影
# =========================================================================


def test_context_session_appends_system_message():
    """system/message 是新增类型：可追加、seq 递增、进事件列表。"""
    session = ContextSession("s", store=None, events=[])
    session.append("session/start", {"session_id": "s", "system_prompt": "T0"})
    event = session.append("system/message", {"content": "T1"})
    assert event.type == "system/message" and event.seq == 2
    assert [e.type for e in session.events] == ["session/start", "system/message"]


def test_context_session_missing_content_is_loud():
    session = ContextSession("s", store=None, events=[])
    with pytest.raises(ValueError, match="缺少必填字段"):
        session.append("system/message", {})


def test_context_session_unknown_type_still_loud():
    """基线的封闭事件表纪律不变：未知类型照旧报错。"""
    session = ContextSession("s", store=None, events=[])
    with pytest.raises(ValueError, match="未知事件类型"):
        session.append("bogus/event", {})


def test_context_session_projection_shadows():
    """投影：最近一次 system/message 遮蔽早先文本；结果里只有一条 system。"""
    session = ContextSession("s", store=None, events=[])
    session.append("session/start", {"session_id": "s", "system_prompt": "T0"})
    session.append("user/message", {"content": "hi"})
    session.append("system/message", {"content": "T1"})
    session.append("assistant/message", {"content": "ok", "tool_calls": []})
    session.append("system/message", {"content": "T2"})

    messages = session.derive_messages()
    systems = [m for m in messages if m.role == "system"]
    assert len(systems) == 1 and systems[0].content == "T2"
    assert [m.role for m in messages] == ["system", "user", "assistant"]


# =========================================================================
# 5) 投影：取值链与"恒一条 system"
# =========================================================================


def test_effective_system_prompt_fallback_chain():
    from harness.session import Event

    def at(seq, type_, data):
        return Event(seq=seq, type=type_, data=data)

    # 都没有 → None
    assert effective_system_prompt([]) is None
    # 只有 session/start → 初始值
    start = at(1, "session/start", {"session_id": "s", "system_prompt": "T0"})
    assert effective_system_prompt([start]) == "T0"
    # 有 system/message → 最近一次生效
    latest = at(2, "system/message", {"content": "T2"})
    assert effective_system_prompt([start, latest]) == "T2"
    # 空串视为"没有系统提示词"
    empty = at(1, "session/start", {"session_id": "s", "system_prompt": ""})
    assert effective_system_prompt([empty]) is None


def test_project_without_system_prompt_keeps_messages():
    from harness.session import Event

    events = [Event(seq=1, type="user/message", data={"content": "hi"})]
    messages = project(events)
    assert [m.role for m in messages] == ["user"]


# =========================================================================
# 6) provider 包装：替换 / 只在变化时记录 / 透传
# =========================================================================


def test_with_system_prompt_replaces_and_inserts():
    original = [system("old"), user("hi")]
    replaced = with_system_prompt(original, "new")
    assert replaced[0].content == "new" and len(replaced) == 2
    assert original[0].content == "old"  # 原列表未被改动

    inserted = with_system_prompt([user("hi")], "new")
    assert [m.role for m in inserted] == ["system", "user"]


def test_provider_records_only_on_change():
    """渲染没变化 → 不重复记录；变化 → 记录一次，并改写发给内层 provider 的请求。"""
    inner = FakeLLM([text_reply("a"), text_reply("b"), text_reply("c")])
    state = {"time": "T1"}
    assembler = PromptAssembler(_reg(Section("env", "时间 {{time}}")))
    renderer = PromptRenderer(assembler, lambda: RuntimeContext("w", "p", state["time"]))
    recorded: list[str] = []
    provider = RuntimePromptProvider(
        inner, renderer, on_render=recorded.append, initial="时间 T1"
    )

    request = LLMRequest(messages=[system("stale"), user("hi")])
    provider.complete(request)
    assert recorded == []  # 渲染 == initial（日志里已有），不重复记

    state["time"] = "T2"
    provider.complete(request)
    assert recorded == ["时间 T2"]
    assert inner.requests[1].messages[0].content == "时间 T2"  # 请求被改写

    provider.complete(request)  # 仍是 T2
    assert recorded == ["时间 T2"]  # 没变化不再记录

    inner.assert_all_consumed()


def test_provider_passes_tools_and_model():
    inner = FakeLLM([text_reply("a")])
    assembler = PromptAssembler(_reg(Section("role", "R")))
    provider = RuntimePromptProvider(
        inner, PromptRenderer(assembler, lambda: RuntimeContext("w", "p", "T")), initial="R"
    )
    request = LLMRequest(messages=[user("hi")], tools=[], model="m-1")
    provider.complete(request)
    assert inner.requests[0].model == "m-1"
    assert inner.requests[0].messages[0].role == "system"


def test_scope_override_flows_through_renderer():
    """作用域遮蔽（_01 机制）在渲染路径上依然生效：渲染用的是遮蔽后的文本。"""
    registry = _reg(Section("env", "工作目录：{{cwd}}。"))
    scope = registry.scoped("session")
    scope.register(Section("env", "改用作用域里的目录：{{cwd}}。"))
    renderer = PromptRenderer(
        PromptAssembler(scope), lambda: RuntimeContext("D:/w", "p", "T")
    )
    assert renderer.render() == "改用作用域里的目录：D:/w。"


def test_projection_of_assistant_tool_calls_survives():
    """扩展投影不破坏基线对 tool_calls 的还原。"""
    session = ContextSession("s", store=None, events=[])
    session.append("session/start", {"session_id": "s", "system_prompt": "T0"})
    session.append(
        "assistant/message",
        {
            "content": "",
            "tool_calls": [{"id": "c1", "name": "calculate", "arguments": {"expression": "1+1"}}],
        },
    )
    messages = session.derive_messages()
    assert messages[-1].tool_calls[0].name == "calculate"
