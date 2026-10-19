"""LLM 层工具调用验收：请求带 tools / 消息序列化 / 流式聚合 / 假 provider 剧本。

钉住四件事：

    1. 请求能把工具声明带给适配器，方言层翻成 OpenAI 兼容的 function 定义；
    2. assistant 的 tool_calls 与 tool 消息的 tool_call_id 能序列化（不丢字段）；
    3. 流式 tool_calls 分片能按 index 正确拼接（含参数被切碎的常见情形）；
    4. 假 provider 能演"先调工具、再给最终回答"，供 agent-loop 测试用。
"""

from __future__ import annotations

import json

import pytest

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    InvalidRequestError,
    LLMRequest,
    Message,
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallAccumulator,
    ToolCallsDelta,
    ToolSpec,
    assemble_text,
    build_payload,
    llm_plugin,
    message_payload,
    parse_stream,
)
from plugins.capability.llm.fake import (
    FakeProvider,
    ScriptedReply,
    fake_model,
)


def messages(*items: Message) -> tuple[Message, ...]:
    return items


def tool_spec() -> ToolSpec:
    return ToolSpec(
        name="read_file",
        description="读取文件",
        parameters={
            "path": {"type": "string", "required": True, "description": "路径"},
            "limit": {"type": "integer", "required": False, "default": 200},
        },
    )


# ---------------------------------------------------------------------------
# 请求 / 序列化
# ---------------------------------------------------------------------------


def test_payload_includes_tools_and_converts_schema():
    request = LLMRequest(
        provider="p",
        model="m",
        messages=messages(Message(role="user", content="读一下 a.txt")),
        tools=(tool_spec(),),
    )
    payload = build_payload(request)
    assert payload["tool_choice"] == "auto"
    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "read_file"
    params = tool["function"]["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["path"]
    assert params["properties"]["path"]["type"] == "string"
    assert params["properties"]["limit"]["default"] == 200


def test_assistant_tool_calls_and_tool_result_serialize():
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="c1", name="read_file", arguments='{"path":"a.txt"}'),),
    )
    result = Message(role="tool", content="文件内容", tool_call_id="c1")

    assistant_payload = message_payload(assistant)
    assert assistant_payload["content"] is None  # 没有文本 → null
    call = assistant_payload["tool_calls"][0]
    assert call["id"] == "c1"
    assert call["type"] == "function"
    assert call["function"] == {"name": "read_file", "arguments": '{"path":"a.txt"}'}

    tool_payload = message_payload(result)
    assert tool_payload["role"] == "tool"
    assert tool_payload["tool_call_id"] == "c1"


def test_assistant_with_text_and_tool_calls_keeps_both():
    message = Message(
        role="assistant",
        content="我看一下",
        tool_calls=(ToolCall(id="c1", name="x"),),
    )
    payload = message_payload(message)
    assert payload["content"] == "我看一下"
    assert payload["tool_calls"]


# ---------------------------------------------------------------------------
# 流式聚合
# ---------------------------------------------------------------------------


def sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(event, ensure_ascii=False)}" for event in events] + [
        "data: [DONE]"
    ]


def call_delta(index: int, *, call_id=None, name=None, arguments=None) -> dict:
    function: dict = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    item: dict = {"index": index, "function": function}
    if call_id is not None:
        item["id"] = call_id
    return {"choices": [{"delta": {"tool_calls": [item]}}]}


def test_stream_aggregates_fragmented_tool_calls():
    lines = sse(
        call_delta(0, call_id="c1", name="read_file", arguments='{"pa'),
        call_delta(0, arguments='th":"a.txt"}'),
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )
    accumulator = ToolCallAccumulator()
    text_parts: list[str] = []
    for chunk in parse_stream(lines):
        if isinstance(chunk, ToolCallsDelta):
            accumulator.feed(chunk.deltas)
        elif isinstance(chunk, TextDelta):
            text_parts.append(chunk.text)

    calls = accumulator.calls()
    assert len(calls) == 1
    assert calls[0].id == "c1"
    assert calls[0].name == "read_file"
    assert calls[0].parsed_arguments() == {"path": "a.txt"}


def test_stream_aggregates_multiple_parallel_calls():
    lines = sse(
        call_delta(0, call_id="a", name="read_file", arguments='{"path":"a"}'),
        call_delta(1, call_id="b", name="list_files", arguments='{"path":"."}'),
    )
    accumulator = ToolCallAccumulator()
    for chunk in parse_stream(lines):
        if isinstance(chunk, ToolCallsDelta):
            accumulator.feed(chunk.deltas)
    calls = accumulator.calls()
    assert [call.name for call in calls] == ["read_file", "list_files"]
    assert [call.id for call in calls] == ["a", "b"]


def test_text_and_tool_calls_in_same_stream():
    lines = sse(
        {"choices": [{"delta": {"content": "我先看看"}}]},
        call_delta(0, call_id="c1", name="read_file", arguments="{}"),
    )
    accumulator = ToolCallAccumulator()
    texts: list[str] = []
    for chunk in parse_stream(lines):
        if isinstance(chunk, TextDelta):
            texts.append(chunk.text)
        elif isinstance(chunk, ToolCallsDelta):
            accumulator.feed(chunk.deltas)
    assert "".join(texts) == "我先看看"
    assert accumulator.calls()[0].name == "read_file"


def test_tool_call_without_id_gets_a_stable_fallback():
    accumulator = ToolCallAccumulator()
    accumulator.feed([{"index": 2, "function": {"name": "x", "arguments": "{}"}}])
    assert accumulator.calls()[0].id == "call_2"


def test_bad_arguments_fail_loud_with_raw_text():
    call = ToolCall(id="c", name="x", arguments="{not json")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        call.parsed_arguments()


# ---------------------------------------------------------------------------
# 路由层校验
# ---------------------------------------------------------------------------


def booted(**config) -> tuple[Context, object]:
    config.setdefault("default_provider", "fake")
    config.setdefault("default_model", "fake-1")
    ctx = Context()
    ctx.load_plugin(llm_plugin(**config))
    return ctx, ctx.require(LLM_SERVICE)


def test_request_carries_tools_to_provider():
    _ctx, llm = booted()
    provider = FakeProvider(replies=["ok"])
    llm.register("fake", provider)

    request = llm.request(
        messages(Message(role="user", content="x")), tools=[tool_spec()]
    )
    list(llm.stream(request))
    assert provider.calls[0].tools[0].name == "read_file"
    assert provider.calls[0].tool_choice is None  # 由适配器默认 auto


def test_model_without_tool_support_rejects_tools():
    _ctx, llm = booted()
    info = fake_model("fake-1")
    no_tools = ModelInfo(
        provider=info.provider,
        model=info.model,
        context_window=info.context_window,
        supports_tools=False,
    )
    llm.register("fake", FakeProvider(models=[no_tools]))
    request = llm.request(
        messages(Message(role="user", content="x")), tools=[tool_spec()]
    )
    with pytest.raises(InvalidRequestError, match="不支持工具调用"):
        list(llm.stream(request))


def test_tool_choice_validation():
    _ctx, llm = booted()
    llm.register("fake", FakeProvider())
    base = messages(Message(role="user", content="x"))

    with pytest.raises(InvalidRequestError, match="没有 tools"):
        list(llm.stream(llm.request(base, tool_choice="auto")))
    with pytest.raises(InvalidRequestError, match="tool_choice"):
        list(
            llm.stream(
                llm.request(base, tools=[tool_spec()], tool_choice="sometimes")
            )
        )


# ---------------------------------------------------------------------------
# 假 provider 剧本：先调工具、再回答
# ---------------------------------------------------------------------------


def test_fake_provider_can_script_a_tool_round_trip():
    _ctx, llm = booted()
    provider = FakeProvider(
        replies=[
            ScriptedReply(
                text="我读一下",
                tool_calls=(ToolCall(id="c1", name="read_file", arguments='{"path":"a.txt"}'),),
            ),
            "文件里是 hello",
        ]
    )
    llm.register("fake", provider)

    first = list(llm.stream(llm.request(messages(Message(role="user", content="读文件")))))
    accumulator = ToolCallAccumulator()
    for chunk in first:
        if isinstance(chunk, ToolCallsDelta):
            accumulator.feed(chunk.deltas)
    calls = accumulator.calls()
    assert calls[0].id == "c1" and calls[0].parsed_arguments() == {"path": "a.txt"}
    assert first[-1].reason == "tool_calls"

    # 第二轮：带工具结果再问
    second_messages = (
        Message(role="user", content="读文件"),
        Message(role="assistant", content="我读一下", tool_calls=calls),
        Message(role="tool", content="hello", tool_call_id="c1"),
    )
    second = list(llm.stream(llm.request(second_messages)))
    assert assemble_text(second) == "文件里是 hello"
    assert second[-1].reason == "stop"


def test_fake_provider_scripted_reply_validation():
    with pytest.raises(ValueError, match="至少要给"):
        ScriptedReply()
