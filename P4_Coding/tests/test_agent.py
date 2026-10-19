"""agent / agent-loop / agent-admin 验收。

重点钉住：
    · agent 注册表是惰性的：没有驱动器 → create 失败；
    · 一轮真 turn：模型 → 工具 → 回灌 → 最终回答，事件按顺序落日志；
    · steer / inject 在 step 边界生效；cancel 协作式；max_steps / budget 收尾；
    · 换一个"一步收尾"的假驱动器，agent 包照常工作（差距 1）；
    · agent-admin 按模式创建 / 停止 / 列举。
"""

from __future__ import annotations

from typing import Any

import pytest

from core import Context
from plugins.base.agent import (
    AGENT_REQUEST,
    AGENT_SERVICE,
    AgentNotAvailable,
    AgentOptions,
    agent_plugin,
)
from plugins.base.llm import LLM_SERVICE, ToolCall, llm_plugin
from plugins.base.prompt import prompt_plugin
from plugins.base.sessions import (
    ASSISTANT,
    SESSION_START,
    SESSIONS_SERVICE,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    USER_MESSAGE,
    sessions_plugin,
)
from plugins.base.tools_admin import TOOL_ADMIN_SERVICE, tool, tools_admin_plugin
from plugins.capability.agent.admin import AGENT_ADMIN_SERVICE, AgentMode, agent_admin_plugin
from plugins.capability.agent.loop import agent_loop_plugin
from plugins.capability.llm.fake import ScriptedReply, fake_llm_plugin

# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def boot(
    tmp_path,
    *,
    replies=None,
    markdown: str | None = None,
    loop_config=None,
    prompt_sections=None,
) -> Context:
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    ctx.load_plugin(fake_llm_plugin(replies=replies or ["你好"]))
    ctx.load_plugin(prompt_plugin(sections=prompt_sections or ()))
    ctx.load_plugin(sessions_plugin())
    ctx.load_plugin(tools_admin_plugin())
    ctx.load_plugin(agent_plugin())
    ctx.load_plugin(agent_loop_plugin(**(loop_config or {})))
    ctx.load_plugin(agent_admin_plugin())
    return ctx


def add_tool(ctx: Context, name: str = "echo"):
    @tool(name)
    def echo(text: str) -> dict[str, Any]:
        """原样返回。"""
        return {"text": text}

    ctx.require(TOOL_ADMIN_SERVICE).register(echo.definition, echo.func, owner="test")
    return echo


def events(ctx: Context, session_id: str) -> list[str]:
    return [event.kind for event in ctx.require(SESSIONS_SERVICE).events(session_id)]


def kinds(ctx: Context, session_id: str) -> list[str]:
    return events(ctx, session_id)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_registry_is_lazy_without_a_driver(tmp_path):
    ctx = Context()
    ctx.load_plugin(agent_plugin())
    with pytest.raises(AgentNotAvailable, match="驱动器"):
        ctx.require(AGENT_SERVICE).create()


def test_create_and_list_agents(tmp_path):
    ctx = boot(tmp_path)
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    handle = admin.create(session_id="s1", agent_id="main")
    assert handle.id == "main"
    assert handle.session_id == "s1"
    assert handle.status() == "idle"
    assert [item.id for item in admin.list()] == ["main"]
    assert ctx.require(AGENT_SERVICE).roots() == [handle]

    assert admin.stop("main") is True
    assert admin.list() == []
    assert handle.status() == "disposed"
    assert admin.stop("main") is False


# ---------------------------------------------------------------------------
# 一轮真 turn
# ---------------------------------------------------------------------------


def test_simple_turn_writes_events_and_returns_text(tmp_path):
    ctx = boot(tmp_path, replies=["你好，我是助手"])
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")
    result = handle.followup("在吗")

    assert result is not None and result.status == "done"
    assert result.final_text == "你好，我是助手"
    assert result.steps == 1

    log = kinds(ctx, "s1")
    assert log[0] == SESSION_START
    assert TURN_START in log and ASSISTANT in log and TURN_END in log
    assert ctx.require(SESSIONS_SERVICE).messages("s1")[-1].content == "你好，我是助手"


def test_tool_round_trip(tmp_path):
    ctx = boot(
        tmp_path,
        replies=[
            ScriptedReply(
                text="我算一下",
                tool_calls=(ToolCall(id="c1", name="calc", arguments='{"expression":"2+3"}'),),
            ),
            "结果是 5",
        ],
    )
    # 用真实的 tool-math 插件
    from plugins.capability.tools.math import tool_math_plugin

    ctx.load_plugin(tool_math_plugin())
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")
    result = handle.followup("2+3 等于几")

    assert result.status == "done"
    assert result.final_text == "结果是 5"
    assert result.steps == 2

    log = kinds(ctx, "s1")
    assert TOOL_CALL in log and TOOL_RESULT in log
    messages = ctx.require(SESSIONS_SERVICE).messages("s1")
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls[0].name == "calc"
    assert messages[2].tool_call_id == "c1"


def test_tool_failure_is_logged_and_loop_continues(tmp_path):
    ctx = boot(
        tmp_path,
        replies=[
            ScriptedReply(tool_calls=(ToolCall(id="c1", name="nope", arguments="{}"),)),
            "工具不存在，我换个办法",
        ],
    )
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")
    result = handle.followup("试试")
    assert result.status == "done"
    results = [
        event.payload for event in ctx.require(SESSIONS_SERVICE).events("s1")
        if event.kind == TOOL_RESULT
    ]
    assert results[0]["status"] == "error"
    assert results[0]["error_code"] == "TOOL_NOT_FOUND"


def test_steer_lands_mid_turn_at_next_step(tmp_path):
    ctx = boot(
        tmp_path,
        replies=[
            ScriptedReply(tool_calls=(ToolCall(id="c1", name="calc", arguments='{"expression":"1+1"}'),)),
            "好的",
        ],
    )
    from plugins.capability.tools.math import tool_math_plugin

    ctx.load_plugin(tool_math_plugin())
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")

    original_run = handle._backend.run_turn

    def run_with_steer(text: str):
        # 在第二轮开始前插一句话（模拟用户在工具执行期间说话）
        handle.steer("顺便算一下 2+2")
        return original_run(text)

    handle._backend = type(handle._backend)(
        session_id=handle._backend.session_id,
        run_turn=run_with_steer,
        steer=handle._backend.steer,
        inject=handle._backend.inject,
        cancel=handle._backend.cancel,
        dispose=handle._backend.dispose,
    )
    handle.followup("开始")
    log = ctx.require(SESSIONS_SERVICE).events("s1")
    steered = [event for event in log if event.kind == USER_MESSAGE]
    assert len(steered) == 1
    assert steered[0].payload["source"] == "steer"
    assert steered[0].payload["text"] == "顺便算一下 2+2"


def test_inject_does_not_need_a_running_turn(tmp_path):
    ctx = boot(tmp_path, replies=["好"])
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")
    handle.inject("背景资料：仓库是 Python 项目")
    handle.followup("开始")
    log = ctx.require(SESSIONS_SERVICE).events("s1")
    injected = [event for event in log if event.kind == USER_MESSAGE]
    assert injected[0].payload["source"] == "inject"
    assert "背景资料" in injected[0].payload["text"]


def test_max_steps_finishes_structurally(tmp_path):
    ctx = boot(
        tmp_path,
        replies=[
            ScriptedReply(tool_calls=(ToolCall(id="c1", name="calc", arguments='{"expression":"1+1"}'),)),
            ScriptedReply(tool_calls=(ToolCall(id="c2", name="calc", arguments='{"expression":"2+2"}'),)),
        ],
    )
    from plugins.capability.tools.math import tool_math_plugin

    ctx.load_plugin(tool_math_plugin())
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")
    handle.options = AgentOptions(max_steps=2, mode=handle.options.mode)
    # max_steps 来自创建时的选项；重建一个受限的 agent
    limited = ctx.require(AGENT_ADMIN_SERVICE).create(
        session_id="s2", max_steps=2, agent_id="limited"
    )
    result = limited.followup("一直算")
    assert result.status == "max_steps"
    assert result.steps == 2


def test_budget_exceeded(tmp_path):
    ctx = boot(tmp_path, replies=["一次就超预算" * 20])
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(
        session_id="s1", token_budget=1, agent_id="b"
    )
    result = handle.followup("你好")
    assert result.status == "budget_exceeded"


def test_system_prompt_is_included_in_model_request(tmp_path):
    """渲染出的系统提示词必须出现在发给模型的 messages 里。"""
    ctx = boot(
        tmp_path,
        prompt_sections=[{"name": "identity", "order": 10, "text": "MARKER-42"}],
    )
    requests = []
    ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

    handle = ctx.require(AGENT_ADMIN_SERVICE).create(
        session_id="s1", agent_id="p"
    )
    handle.followup("你好")

    assert requests, "没有观察到模型请求"
    first = requests[0].messages[0]
    assert first.role == "system"
    assert "MARKER-42" in first.text


def test_budget_stop_does_not_leave_dangling_tool_calls(tmp_path):
    """预算到顶时，本步已经落盘的 tool_calls 必须先执行完再停。

    否则历史里会留下"assistant 带 tool_calls、却没有 tool 结果"的悬空调用，
    下一轮 OpenAI 兼容请求会被直接拒绝。
    """
    from plugins.base.llm import Finish, ToolCallsDelta, Usage

    class ToolCallProvider:
        name = "tools"

        def models(self):
            from plugins.capability.llm.fake import fake_model

            return {info.model: info for info in [fake_model("m", provider="tools")]}

        def stream(self, request):
            yield ToolCallsDelta(
                deltas=(
                    {
                        "index": 0,
                        "id": "c1",
                        "function": {"name": "echo", "arguments": '{"text":"x"}'},
                    },
                )
            )
            yield Finish(reason="tool_calls", usage=Usage(input_tokens=100))

    ctx = boot(tmp_path)
    add_tool(ctx)
    ctx.require(LLM_SERVICE).register("tools", ToolCallProvider())
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(
        session_id="s1",
        provider="tools",
        model="m",
        token_budget=1,
        agent_id="t",
    )
    result = handle.followup("调用工具")
    assert result.status == "budget_exceeded"

    messages = ctx.require(SESSIONS_SERVICE).messages("s1")
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            assert any(later.role == "tool" for later in messages[index + 1 :]), (
                "assistant 的 tool_calls 没有配套 tool 结果"
            )
    assert [k for k in events(ctx, "s1")].count(TOOL_RESULT) == 1


def test_cancel_is_cooperative_and_keeps_partial_text(tmp_path):
    from plugins.base.llm import Finish, TextDelta

    class SlowProvider:
        name = "slow"

        def models(self):
            from plugins.capability.llm.fake import fake_model

            return {info.model: info for info in [fake_model("m", provider="slow")]}

        def stream(self, request):
            yield TextDelta("第一段")
            yield TextDelta("第二段")
            yield Finish(reason="stop")

    ctx = boot(tmp_path)
    unregister = ctx.require(LLM_SERVICE).register("slow", SlowProvider())
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(
        session_id="s1", provider="slow", model="m", agent_id="c"
    )

    # 在**第一段吐出之后**取消：已生成的部分必须留在会话里。
    class CancellingProvider:
        name = "slow"

        def models(self):
            from plugins.capability.llm.fake import fake_model

            return {info.model: info for info in [fake_model("m", provider="slow")]}

        def stream(self, request):
            yield TextDelta("第一段")
            handle.cancel()
            yield TextDelta("第二段")
            yield Finish(reason="stop")

    unregister()
    ctx.require(LLM_SERVICE).register("slow", CancellingProvider())

    result = handle.followup("说点什么")
    assert result.status == "cancelled"
    assert result.final_text == "第一段"
    messages = ctx.require(SESSIONS_SERVICE).messages("s1")
    assistant = [m for m in messages if m.role == "assistant"]
    assert assistant and assistant[-1].text == "第一段"
    assert assistant[-1].tool_calls == ()


# ---------------------------------------------------------------------------
# 换驱动器（差距 1）
# ---------------------------------------------------------------------------


def test_agent_works_with_a_different_driver(tmp_path):
    """一个"一步就收尾"的假驱动器：agent 包与日志照常工作。"""
    from plugins.base.agent import AgentBackend, AgentHandle, TurnResult

    ctx = boot(tmp_path)
    registry = ctx.require(AGENT_SERVICE)
    registry._factory = None  # 卸掉默认驱动器

    sessions = ctx.require(SESSIONS_SERVICE)

    class OneShot:
        def create(self, options, *, session_id, agent_id, parent):
            resolved = session_id or f"{agent_id}-session"
            sessions.open(resolved, system_prompt="一步循环")
            def run_turn(text: str) -> TurnResult:
                sessions.append(resolved, TURN_START, {"turn": 1, "text": text})
                sessions.append(
                    resolved, ASSISTANT, {"content": "一步回答", "tool_calls": []}
                )
                sessions.append(
                    resolved,
                    TURN_END,
                    {"turn": 1, "status": "done", "steps": 1, "final_text": "一步回答"},
                )
                return TurnResult(turn=1, status="done", final_text="一步回答", steps=1)

            backend = AgentBackend(
                session_id=resolved,
                run_turn=run_turn,
                steer=lambda text: None,
                inject=lambda text: None,
                cancel=lambda: True,
                dispose=lambda: None,
            )
            handle = AgentHandle(agent_id, options, backend, parent=parent)
            handle._emit = ctx.emit
            return handle

        def resume(self, session_id, options, *, agent_id, parent):
            raise NotImplementedError

    registry.register_factory(OneShot())
    handle = registry.create(session_id="s1")
    result = handle.followup("你好")
    assert result.final_text == "一步回答"
    assert kinds(ctx, "s1")[-3:] == [TURN_START, ASSISTANT, TURN_END]


# ---------------------------------------------------------------------------
# agent-admin 模式
# ---------------------------------------------------------------------------


def test_modes_and_default(tmp_path):
    ctx = boot(tmp_path)
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    assert {"default", "quick", "careful"} <= set(admin.modes())
    assert admin.default_mode == "default"
    assert admin.describe("quick").max_steps == 3

    admin.set_default("quick")
    assert admin.default_mode == "quick"
    assert admin.describe("careful").token_budget == 100_000


def test_unknown_and_duplicate_mode_handling(tmp_path):
    ctx = boot(tmp_path)
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    with pytest.raises(ValueError, match="模式不存在"):
        admin.describe("nope")
    admin.define(AgentMode(name="custom", description="自定义", max_steps=5))
    assert "custom" in admin.modes()
    with pytest.raises(ValueError, match="默认模式"):
        admin.remove("default")
    admin.remove("custom")


def test_create_with_mode_applies_options(tmp_path):
    ctx = boot(tmp_path, replies=["ok"])
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    handle = admin.create(mode="quick", session_id="s1")
    assert handle.options.mode == "quick"
    assert handle.options.max_steps == 3


def test_overrides_beat_mode(tmp_path):
    ctx = boot(tmp_path, replies=["ok"])
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    handle = admin.create(mode="quick", session_id="s1", max_steps=9)
    assert handle.options.max_steps == 9


def test_mode_with_character_sets_it_on_the_session(tmp_path):
    from plugins.capability.prompt.character_admin import character_admin_plugin

    ctx = boot(tmp_path, replies=["ok"])
    ctx.load_plugin(character_admin_plugin())
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    admin.define(AgentMode(name="coder", description="写代码", character="coder"))
    handle = admin.create(mode="coder", session_id="s1")
    characters = ctx.require("character-admin")
    assert characters.active_name(handle.session_id) == "coder"


def test_snapshot(tmp_path):
    ctx = boot(tmp_path, replies=["ok"])
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    admin.create(session_id="s1", agent_id="a1")
    snap = admin.snapshot()
    assert snap["default_mode"] == "default"
    assert snap["agents"][0]["id"] == "a1"
    assert any(mode["name"] == "quick" for mode in snap["modes"])


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def test_resume_reopens_the_session(tmp_path):
    ctx = boot(tmp_path, replies=["第一次", "第二次"])
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    first = admin.create(session_id="s1", agent_id="a1")
    first.followup("你好")
    first.dispose()

    resumed = admin.resume("s1", agent_id="a2")
    result = resumed.followup("继续")
    assert result.status == "done"
    messages = ctx.require(SESSIONS_SERVICE).messages("s1")
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0].content == "你好"


# ---------------------------------------------------------------------------
# 安全上限
# ---------------------------------------------------------------------------


def test_safety_max_steps_prevents_runaway_loops(tmp_path):
    """模型一直要求调工具时，循环必须有上限（默认 64），不能无限跑。"""
    ctx = boot(
        tmp_path,
        replies=[
            ScriptedReply(tool_calls=(ToolCall(id="c1", name="calc", arguments='{"expression":"1+1"}'),))
        ],
        loop_config={"default_max_steps": 3},
    )
    from plugins.capability.tools.math import tool_math_plugin

    ctx.load_plugin(tool_math_plugin())
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="s1")
    result = handle.followup("一直算")
    assert result.status == "max_steps"
    assert result.steps == 3


def test_unlimited_steps_requires_explicit_null(tmp_path):
    """显式 `default_max_steps=None` 才是不限（默认有安全上限）。"""
    from plugins.capability.agent.loop import AgentLoopPlugin

    assert AgentLoopPlugin.SAFETY_MAX_STEPS == 64
    assert AgentLoopPlugin()._default_max_steps == 64
    assert AgentLoopPlugin(default_max_steps=None)._default_max_steps is None


# ---------------------------------------------------------------------------
# 路由不可用：可读错误 + 显式模型是出路
# ---------------------------------------------------------------------------


def test_session_recorded_route_missing_gives_actionable_error(tmp_path):
    """会话用 fake 建的，换成只装了 real 的装配 → 报错要说清怎么处理。"""
    from plugins.base.sessions import SESSIONS_SERVICE as SESSIONS_SVC

    ctx = boot(tmp_path, replies=["离线回答"])
    admin = ctx.require(AGENT_ADMIN_SERVICE)
    handle = admin.create(session_id="mixed", agent_id="offline")
    handle.followup("离线一句")
    handle.dispose()

    # 换一个装配：只注册 real 路由
    from plugins.base.agent import agent_plugin as base_agent_plugin
    from plugins.capability.agent.admin import agent_admin_plugin as admin_plugin
    from plugins.capability.agent.loop import agent_loop_plugin as loop_plugin
    from plugins.capability.llm.fake import FakeProvider, fake_model

    ctx2 = Context()
    ctx2.load_plugin(llm_plugin(default_provider="real", default_model="real-1"))
    ctx2.require(LLM_SERVICE).register(
        "real", FakeProvider(models=[fake_model("real-1", provider="real")], replies=["真实"])
    )
    ctx2.load_plugin(prompt_plugin())
    ctx2.load_plugin(sessions_plugin())
    # 把旧会话灌进去（模拟磁盘恢复）
    old = ctx.require(SESSIONS_SVC)
    ctx2.require(SESSIONS_SVC)._store.restore("mixed", list(old.events("mixed")))
    ctx2.load_plugin(tools_admin_plugin())
    ctx2.load_plugin(base_agent_plugin())
    ctx2.load_plugin(loop_plugin())
    ctx2.load_plugin(admin_plugin())

    with pytest.raises(RuntimeError) as err:
        ctx2.require(AGENT_ADMIN_SERVICE).resume("mixed", agent_id="online")
    message = str(err.value)
    assert "模型路由不可用" in message
    assert "会话记录" in message
    assert "--provider" in message          # 给出可操作的出路
    assert "sessions/mixed.jsonl" in message


def test_explicit_provider_overrides_session_record(tmp_path):
    """显式给 provider/model 时，不再读会话记录（这是换模型的正确姿势）。"""
    ctx = boot(tmp_path, replies=["离线回答"])
    handle = ctx.require(AGENT_ADMIN_SERVICE).create(session_id="mixed", agent_id="a")
    handle.followup("离线一句")
    handle.dispose()

    # 同一装配里再注册一条 real 路由
    from plugins.capability.llm.fake import FakeProvider, fake_model

    ctx.require(LLM_SERVICE).register(
        "real", FakeProvider(models=[fake_model("real-1", provider="real")], replies=["真实"])
    )
    resumed = ctx.require(AGENT_ADMIN_SERVICE).resume(
        "mixed", agent_id="b", provider="real", model="real-1"
    )
    result = resumed.followup("真实一句")
    assert result.final_text == "真实"
