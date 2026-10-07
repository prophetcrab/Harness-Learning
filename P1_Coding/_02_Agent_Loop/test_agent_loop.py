"""_02_Agent_Loop 的验收测试：单元级、全离线、FakeLLM 驱动。

覆盖五层（对应学习计划 M1 的验收 + _02 的新能力）：
1. 两步收尾：工具申请 → 结果 → 最终回答
2. 轨迹：TurnResult/Step/ToolResult 三层对象可事后审查
3. turn/step 词汇：一个 turn 多个 step；多 turn 历史累积
4. 触顶停止：超出 step 预算结构化终止，不死循环
5. 工具报错恢复：错误回填、执行器崩溃被包装，不炸循环
6. 取消：cancel() 与 should_stop 回调；事件流按序触发
7. 纪律：请求快照独立、max_steps 校验 fail loud

运行：cd P1_Coding/_02_Agent_Loop && python -m pytest -q
"""

import pytest

from agent_loop import AgentLoop
from llm_seam import (
    FakeLLM,
    build_default_toolbox,
    text_reply,
    tool_call_reply,
    tool_calls_reply,
)


# =========================================================================
# 1) 两步收尾：核心闭环
# =========================================================================


def test_two_step_completion():
    """工具申请 → 工具执行 → 结果回填 → 最终回答。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("结果是 70066.52"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute, system_prompt="你是计算助手")

    result = loop.run("帮我算 1234*56.78", tools=toolbox.specs())

    assert result.status == "done"
    assert result.turn == 1
    assert len(result.steps) == 2
    assert result.final_text == "结果是 70066.52"
    provider.assert_all_consumed()


# =========================================================================
# 2) 轨迹：三层对象可事后审查
# =========================================================================


def test_trace_is_first_class():
    """每个 step 的请求/回复/工具结果都对得上，且请求快照反映当时的历史。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "2+2"}, call_id="c1"),
            text_reply("4"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)

    result = loop.run("2+2", tools=toolbox.specs())

    step1 = result.steps[0]
    assert step1.index == 1
    assert step1.response.message.tool_calls[0].name == "calculate"
    # 第 1 步发出时历史只有 user 一条（工具还没发生）
    assert [m.role for m in step1.request.messages] == ["user"]
    assert len(step1.tool_results) == 1
    assert step1.tool_results[0].call_id == "c1"
    assert step1.tool_results[0].error is False
    assert step1.tool_results[0].result["result"] == 4

    step2 = result.steps[1]
    assert step2.index == 2
    assert step2.tool_results == []
    # 第 2 步发出时历史已含 user/assistant/tool 三条
    assert [m.role for m in step2.request.messages] == ["user", "assistant", "tool"]


# =========================================================================
# 3) turn/step 词汇
# =========================================================================


def test_multiple_steps_in_one_turn():
    """一个 turn 里可以有多个 step（多轮工具调用），turn 与 step 不是一回事。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "987654*321"}),
            tool_call_reply("calculate", {"expression": "317036934/7"}),
            text_reply("答案是 45290990.57"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)

    result = loop.run("987654*321 再除以 7", tools=toolbox.specs())

    assert result.status == "done"
    assert result.turn == 1
    assert len(result.steps) == 3  # 三次模型往返 = 3 个 step，但都在同一个 turn 里


def test_multi_turn_history_accumulates():
    """多 turn：历史跨 turn 累积，system 提示只注入一次。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1+1"}),
            text_reply("2"),
            text_reply("不客气！"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute, system_prompt="你是助手")

    first = loop.run("1+1", tools=toolbox.specs())
    second = loop.run("谢谢", tools=toolbox.specs())

    assert first.turn == 1
    assert second.turn == 2
    # 第二个 turn 的历史包含第一个 turn 的全部消息
    assert len(second.messages) > len(first.messages)
    assert [m.role for m in second.messages].count("system") == 1


# =========================================================================
# 4) 触顶停止：步数预算兜底
# =========================================================================


def test_max_steps_structured_stop():
    """模型停不下来时返回 max_steps，不死循环、不抛异常。"""
    provider = FakeLLM([tool_call_reply("calculate", {"expression": "1+1"})] * 5)
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute, max_steps=3)

    result = loop.run("循环", tools=toolbox.specs())

    assert result.status == "max_steps"
    assert len(result.steps) == 3
    assert result.final_text == ""
    assert provider.request_count == 3


# =========================================================================
# 5) 工具报错恢复
# =========================================================================


def test_tool_error_is_fed_back_and_loop_recovers():
    """工具报错不中断：错误回填给模型，模型据此修正并收尾。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1/0"}),
            text_reply("除零无意义，换一个式子吧。"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)

    result = loop.run("算 1/0", tools=toolbox.specs())

    assert result.status == "done"
    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is True
    assert "division by zero" in tool_result.result["error"]
    assert result.final_text.startswith("除零")


def test_tool_exception_is_wrapped():
    """执行器直接抛异常也被包装成错误结果，不炸掉循环。"""
    def exploding(name: str, args: dict) -> dict:
        raise RuntimeError("模拟工具崩溃")

    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1+1"}),
            text_reply("工具坏了。"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, exploding)

    result = loop.run("随便算", tools=toolbox.specs())

    assert result.status == "done"
    assert "模拟工具崩溃" in result.steps[0].tool_results[0].result["error"]


# =========================================================================
# 6) 事件流与取消
# =========================================================================


def test_event_callback_fires_in_order():
    """观察者按序收到 turn/step/tool 事件。"""
    events: list[str] = []
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1+1"}),
            text_reply("2"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(
        provider, toolbox.execute, on_event=lambda kind, payload: events.append(kind)
    )

    loop.run("1+1", tools=toolbox.specs())

    assert events == [
        "turn_start",
        "step_request", "step_response", "tool_result",
        "step_request", "step_response",
        "turn_end",
    ]


def test_cancel_before_step():
    """cancel() 在下一个 step 前生效，返回 cancelled，不调模型。"""
    provider = FakeLLM([text_reply("不该出现")])
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)
    loop.cancel()

    result = loop.run("hi", tools=[])

    assert result.status == "cancelled"
    assert result.steps == []
    assert provider.request_count == 0


def test_should_stop_mid_turn():
    """should_stop 回调在 step 边界检查，可中途打断。"""
    counter = {"n": 0}

    def should_stop() -> bool:
        counter["n"] += 1
        return counter["n"] >= 2  # 放行第 1 步，第 2 步之前停下

    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1+1"}),
            text_reply("2"),  # 永远不会到达
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)

    result = loop.run("1+1", tools=toolbox.specs(), should_stop=should_stop)

    assert result.status == "cancelled"
    assert len(result.steps) == 1
    assert provider.request_count == 1
    assert provider.remaining == 1  # 剧本还剩一条没用


# =========================================================================
# 7) 并行工具调用 + 快照独立 + fail loud
# =========================================================================


def test_parallel_tool_calls_recorded_per_step():
    """一轮申请两个工具：同一个 step 里记录两条 tool_result。"""
    provider = FakeLLM(
        [
            tool_calls_reply(
                [("calculate", {"expression": "1+1"}), ("calculate", {"expression": "2*3"})]
            ),
            text_reply("2 和 6"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)

    result = loop.run("算两个", tools=toolbox.specs())

    step1 = result.steps[0]
    assert len(step1.tool_results) == 2
    assert [t.call_id for t in step1.tool_results] == ["call_1", "call_2"]


def test_request_snapshot_isolated():
    """请求快照独立：后续 append 不倒灌进已发出的请求对象。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "2+2"}),
            text_reply("4"),
        ]
    )
    toolbox = build_default_toolbox()
    loop = AgentLoop(provider, toolbox.execute)

    loop.run("2+2", tools=toolbox.specs())

    assert len(provider.requests[0].messages) == 1  # 第 1 步：只有 user
    assert len(provider.requests[1].messages) == 3  # 第 2 步：user/assistant/tool


def test_max_steps_must_be_positive():
    """fail loud：max_steps < 1 直接报错，不静默接受。"""
    with pytest.raises(ValueError, match="max_steps"):
        AgentLoop(FakeLLM([]), lambda name, args: {}, max_steps=0)
