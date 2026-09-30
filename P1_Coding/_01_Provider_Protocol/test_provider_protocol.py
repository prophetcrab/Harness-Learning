"""_01_Provider_Protocol 的验收测试：单元级、全离线、不需要 key。

覆盖四层：
1. 协议层：两个 provider 都满足 LLMProvider 协议（运行时结构检查）
2. 翻译层：DeepSeek 的方言翻译是纯函数，用假对象直接驱动
3. 闭环层：run_tool_loop 的正确性（工具闭环、并行调用、错误恢复、触顶）
4. 纪律层：FakeLLM 的"剧本正好用完"能抓住调用次数错误

运行：../../.venv/Scripts/python.exe -m pytest -q
"""

from types import SimpleNamespace

import pytest

from llm_seam import (
    FakeLLM,
    LLMProvider,
    ToolCall,
    ToolSpec,
    assistant,
    run_tool_loop,
    system,
    text_reply,
    tool_call_reply,
    tool_calls_reply,
    tool_result,
    user,
)
from llm_seam.deepseek import message_to_wire, response_from_api, tool_to_wire
from llm_seam.tools import build_default_toolbox, calculate


# =========================================================================
# 1) 协议层：FakeLLM 满足 LLMProvider 协议（消费者能无差别使用）
# =========================================================================


def test_fake_llm_satisfies_provider_protocol():
    """runtime_checkable 的结构检查：FakeLLM 就是合法的 provider。"""
    provider = FakeLLM([text_reply("你好")])
    assert isinstance(provider, LLMProvider)


# =========================================================================
# 2) 翻译层：方言翻译的纯函数（双向、可离线验证）
# =========================================================================


def test_message_to_wire_translates_all_roles():
    """四种角色 → 线上格式：assistant 的参数变 JSON 字符串，tool 带配对 id。"""
    assert message_to_wire(system("提示")) == {"role": "system", "content": "提示"}
    assert message_to_wire(user("问题")) == {"role": "user", "content": "问题"}

    assistant_wire = message_to_wire(
        assistant("我来算", [ToolCall(id="c1", name="calculate", arguments={"expression": "1+1"})])
    )
    assert assistant_wire["tool_calls"][0]["function"]["name"] == "calculate"
    # 关键断言：字典被翻译成 JSON 字符串（线上协议要求）
    assert assistant_wire["tool_calls"][0]["function"]["arguments"] == '{"expression": "1+1"}'

    assert message_to_wire(tool_result("c1", {"result": 2})) == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": '{"result": 2}',
    }


def test_response_from_api_translates_reply():
    """线上返回 → 中立响应：arguments 从 JSON 字符串变回字典。

    用 SimpleNamespace 伪造 SDK 对象——翻译函数不依赖 openai 的类定义。
    """
    fake_api_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,  # 纯工具申请时 content 为 None
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            function=SimpleNamespace(
                                name="calculate",
                                arguments='{"expression": "1234*56.78"}',
                            ),
                        )
                    ],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )

    response = response_from_api(fake_api_response)

    assert response.message.role == "assistant"
    assert response.message.content == ""  # None 被规范化为空串
    assert response.finish_reason == "tool_calls"
    assert response.message.tool_calls[0].arguments == {"expression": "1234*56.78"}
    assert response.usage.prompt_tokens == 100


def test_tool_to_wire_translates_spec():
    """工具说明书 → 线上 tools 字段。"""
    spec = ToolSpec(
        name="calculate",
        description="算数",
        parameters={"type": "object", "properties": {}},
    )
    wire = tool_to_wire(spec)
    assert wire["type"] == "function"
    assert wire["function"]["name"] == "calculate"


# =========================================================================
# 3) 闭环层：run_tool_loop 的行为（FakeLLM 驱动，全离线）
# =========================================================================


def test_loop_executes_tool_and_returns_final_answer():
    """核心闭环：模型申请 → 工具执行 → 结果回填 → 模型给最终回答。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("结果是 70066.52"),
        ]
    )

    result = run_tool_loop(
        provider=provider,
        messages=[system("计算助手"), user("帮我算 1234*56.78")],
        tools=build_default_toolbox().specs(),
        execute_tool=build_default_toolbox().execute,
    )

    assert result.status == "done"
    assert result.steps == 2
    assert result.final_text == "结果是 70066.52"

    # 校验第 2 次请求里工具结果的配对与内容
    second_request = provider.requests[1]
    assert [m.role for m in second_request.messages] == [
        "system", "user", "assistant", "tool",
    ]
    tool_message = second_request.messages[3]
    assert tool_message.tool_call_id == "call_1"
    assert "70066.52" in tool_message.content

    provider.assert_all_consumed()


def test_loop_handles_parallel_tool_calls():
    """一轮申请两个工具：逐个执行、逐个回填 tool 消息。"""
    provider = FakeLLM(
        [
            tool_calls_reply([("calculate", {"expression": "1+1"}), ("calculate", {"expression": "2*3"})]),
            text_reply("答案：2 和 6"),
        ]
    )
    toolbox = build_default_toolbox()

    result = run_tool_loop(
        provider=provider,
        messages=[user("算两个式子")],
        tools=toolbox.specs(),
        execute_tool=toolbox.execute,
    )

    assert result.status == "done"
    # 历史：user, assistant(2 个申请), tool, tool, assistant
    assert [m.role for m in result.messages] == ["user", "assistant", "tool", "tool", "assistant"]
    tool_ids = [m.tool_call_id for m in result.messages if m.role == "tool"]
    assert tool_ids == ["call_1", "call_2"]  # id 配对正确


def test_tool_error_is_fed_back_and_loop_recovers():
    """工具报错不中断闭环：错误作为结果回填，模型可以据此修正。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1/0"}),
            text_reply("除零没有定义，换个式子吧。"),
        ]
    )
    toolbox = build_default_toolbox()

    result = run_tool_loop(
        provider=provider,
        messages=[user("算 1/0")],
        tools=toolbox.specs(),
        execute_tool=toolbox.execute,
    )

    assert result.status == "done"
    error_message = [m for m in result.messages if m.role == "tool"][0]
    assert "division by zero" in error_message.content
    assert result.final_text.startswith("除零")

    # 对照：如果工具直接抛异常，闭环的兜底也会把它转成错误结果（不炸循环）
    def exploding_tool(name: str, arguments: dict) -> dict:
        raise RuntimeError("模拟工具崩溃")

    provider_crash = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1+1"}),
            text_reply("工具似乎坏了。"),
        ]
    )
    crash_result = run_tool_loop(
        provider=provider_crash,
        messages=[user("随便算")],
        tools=toolbox.specs(),
        execute_tool=exploding_tool,
    )
    assert crash_result.status == "done"
    assert "模拟工具崩溃" in [m for m in crash_result.messages if m.role == "tool"][0].content


def test_loop_stops_at_max_steps():
    """模型停不下来时，步数预算兜底：返回 max_steps 而不是死循环。"""
    provider = FakeLLM(
        [tool_call_reply("calculate", {"expression": "1+1"})] * 3  # 永远在申请工具
    )
    toolbox = build_default_toolbox()

    result = run_tool_loop(
        provider=provider,
        messages=[user("无限循环测试")],
        tools=toolbox.specs(),
        execute_tool=toolbox.execute,
        max_steps=3,
    )

    assert result.status == "max_steps"
    assert result.steps == 3
    assert provider.request_count == 3


def test_request_snapshot_is_not_mutated_by_later_history():
    """请求快照独立：闭环后续 append 不会倒灌进已发出的请求对象。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "2+2"}),
            text_reply("4"),
        ]
    )
    toolbox = build_default_toolbox()

    run_tool_loop(
        provider=provider,
        messages=[user("2+2")],
        tools=toolbox.specs(),
        execute_tool=toolbox.execute,
    )

    # 第 1 次请求发出时历史只有 1 条（user）；之后历史增长到 3 条
    # （user, assistant, tool），但第 1 次请求的快照必须仍然是 1 条。
    assert len(provider.requests[0].messages) == 1
    assert len(provider.requests[1].messages) == 3


# =========================================================================
# 4) 纪律层：剧本纪律能抓住"调用次数错误"
# =========================================================================


def test_script_exhaustion_is_loud():
    """剧本用完还调用模型 → 立即报错（不少调、也不多调）。"""
    provider = FakeLLM([])  # 空剧本
    with pytest.raises(AssertionError, match="剧本已用完"):
        run_tool_loop(
            provider=provider,
            messages=[user("hi")],
            tools=[],
            execute_tool=lambda name, args: {},
        )


def test_script_leftover_is_loud():
    """剧本没用完 → assert_all_consumed 报错（循环提前结束了）。"""
    provider = FakeLLM([text_reply("一次就答完了")])
    run_tool_loop(
        provider=provider,
        messages=[user("hi")],
        tools=[],
        execute_tool=lambda name, args: {},
    )
    provider.assert_all_consumed()  # 这次恰好用完，不应抛错

    provider_leftover = FakeLLM([text_reply("a"), text_reply("b")])
    run_tool_loop(
        provider=provider_leftover,
        messages=[user("hi")],
        tools=[],
        execute_tool=lambda name, args: {},
    )
    with pytest.raises(AssertionError, match="剧本还剩"):
        provider_leftover.assert_all_consumed()


# =========================================================================
# 5) 工具层：安全计算器（从 P0 沿用，此处防回归）
# =========================================================================


def test_calculator_still_safe():
    """白名单求值：正常计算通过，函数调用与指数炸弹被拒。"""
    assert calculate("1234*56.78")["result"] == pytest.approx(70066.52)
    with pytest.raises(ValueError, match="不支持的语法"):
        calculate("__import__('os')")
    with pytest.raises(ValueError, match="指数超出上限"):
        calculate("9**9**9")
