"""验收：模型选择是数据（0044）——会话级与子代理级切换。

这里不依赖 agent-loop（M2 后续才写），只钉住"选择机制"本身：

    · 两个会话各用各的模型，互不影响；
    · 同一个会话中途换模型（会话记录的是"当前选择"）；
    · 子代理拿父级的选择作基线，可用 agent_options 覆盖 provider/model；
    · 只换路由不显式给 reasoning_effort 时，继承值被清掉（dsh 的规则）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from core import Context
from plugins.base.llm import LLM_SERVICE, Message, assemble_text, llm_plugin
from plugins.capability.llm.fake import FakeProvider, fake_model


def messages(text: str) -> tuple[Message, ...]:
    return (Message(role="user", content=text),)


def build() -> tuple[Context, object]:
    """一个路由层 + 两条路由（每路由各有 small/large 两个模型）。"""
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fast", default_model="fast-small"))
    llm = ctx.require(LLM_SERVICE)
    llm.register(
        "fast",
        FakeProvider(
            models=[
                fake_model("fast-small", provider="fast", context_window=32_000),
                fake_model("fast-large", provider="fast", context_window=128_000),
            ],
            replies=["fast 的回复"],
        ),
    )
    llm.register(
        "reason",
        FakeProvider(
            models=[
                fake_model(
                    "reason-1",
                    provider="reason",
                    context_window=1_000_000,
                    reasoning_efforts=("low", "high"),
                )
            ],
            replies=["reason 的回复"],
        ),
    )
    return ctx, llm


@dataclass
class _Selection:
    """模拟"会话记录的选择"——真实实现里这会是 session/start 事件里的字段。"""

    provider: str
    model: str
    reasoning_effort: str | None = None


def ask(llm, selection: _Selection, text: str, **overrides):
    """按"会话选择 + 本次覆盖"构造请求——覆盖只影响这一次调用。"""
    options = {
        "provider": selection.provider,
        "model": selection.model,
        "reasoning_effort": selection.reasoning_effort,
    }
    options.update(overrides)
    request = llm.request(messages(text), **options)
    chunks = list(llm.stream(request))
    return assemble_text(chunks)


# ---------------------------------------------------------------------------
# 会话级
# ---------------------------------------------------------------------------


def test_two_sessions_can_use_different_models():
    _ctx, llm = build()
    session_a = _Selection("fast", "fast-small")
    session_b = _Selection("reason", "reason-1", reasoning_effort="high")

    assert ask(llm, session_a, "甲") == "fast 的回复"
    assert ask(llm, session_b, "乙") == "reason 的回复"

    # 两次请求确实各走了各的路由与模型
    fake_a = llm._providers["fast"]
    fake_b = llm._providers["reason"]
    assert fake_a.calls[-1].model == "fast-small"
    assert fake_b.calls[-1].model == "reason-1"
    assert fake_b.calls[-1].reasoning_effort == "high"


def test_session_can_switch_model_mid_conversation():
    _ctx, llm = build()
    selection = _Selection("fast", "fast-small")
    ask(llm, selection, "第一句")

    selection.model = "fast-large"  # 会话中途换模型
    ask(llm, selection, "第二句")

    fast = llm._providers["fast"]
    assert [call.model for call in fast.calls] == ["fast-small", "fast-large"]


def test_model_capability_is_resolved_per_selection():
    """上下文窗口按**选中的模型**解析——切模型时窗口跟着变（compact 靠这个）。"""
    _ctx, llm = build()
    assert llm.resolve("fast", "fast-small").context_window == 32_000
    assert llm.resolve("fast", "fast-large").context_window == 128_000
    assert llm.resolve("reason", "reason-1").context_window == 1_000_000


# ---------------------------------------------------------------------------
# 子代理级
# ---------------------------------------------------------------------------


def child_selection(parent: _Selection, **agent_options) -> _Selection:
    """子代理的选择：父级最近一次请求作基线，agent_options 覆盖。

    这是 0044 的规则；dsh 的实现在 subagent 包里，规则相同。
    """
    provider = agent_options.get("provider", parent.provider)
    model = agent_options.get("model", parent.model)
    effort = agent_options.get("reasoning_effort", parent.reasoning_effort)
    if "provider" in agent_options and "reasoning_effort" not in agent_options:
        # 只换路由：继承来的档位属于旧模型，清掉让新模型用自己的默认值。
        effort = None
    return _Selection(provider, model, effort)


def test_subagent_inherits_parent_model_by_default():
    _ctx, llm = build()
    parent = _Selection("fast", "fast-small")
    child = child_selection(parent)
    assert ask(llm, child, "子任务") == "fast 的回复"
    assert llm._providers["fast"].calls[-1].model == "fast-small"


def test_subagent_can_override_model():
    _ctx, llm = build()
    parent = _Selection("fast", "fast-small")
    child = child_selection(parent, provider="reason", model="reason-1")

    assert ask(llm, child, "子任务") == "reason 的回复"
    assert llm._providers["reason"].calls[-1].model == "reason-1"
    # 父级的选择没有被改动
    assert (parent.provider, parent.model) == ("fast", "fast-small")


def test_subagent_route_switch_clears_inherited_reasoning_effort():
    """dsh 的规则：换路由不显式给档位 → 清掉继承值（避免把旧模型的档位塞给新模型）。"""
    _ctx, llm = build()
    parent = _Selection("reason", "reason-1", reasoning_effort="high")
    child = child_selection(parent, provider="fast", model="fast-small")
    assert child.reasoning_effort is None

    ask(llm, child, "子任务")
    assert llm._providers["fast"].calls[-1].reasoning_effort is None


def test_subagent_can_keep_effort_on_same_route():
    _ctx, llm = build()
    parent = _Selection("reason", "reason-1", reasoning_effort="high")
    child = child_selection(parent, model="reason-1")
    assert child.reasoning_effort == "high"


# ---------------------------------------------------------------------------
# 请求级覆盖（临时切换）
# ---------------------------------------------------------------------------


def test_request_level_override_for_one_call():
    _ctx, llm = build()
    selection = _Selection("fast", "fast-small")
    ask(llm, selection, "常规")
    ask(llm, selection, "用大模型这一次", model="fast-large")
    ask(llm, selection, "又回来了")

    fast = llm._providers["fast"]
    assert [call.model for call in fast.calls] == [
        "fast-small",
        "fast-large",
        "fast-small",
    ]


def test_invalid_override_fails_loud():
    _ctx, llm = build()
    selection = _Selection("fast", "fast-small")
    with pytest.raises(Exception, match="没有模型|不支持"):
        ask(llm, selection, "x", model="不存在的模型")
