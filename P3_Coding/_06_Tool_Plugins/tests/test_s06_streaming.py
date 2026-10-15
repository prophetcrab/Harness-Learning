"""`_02` 流式测试：聚合器、流式/非流式一致、增量事件、取消、日志不受影响。

一条纪律贯穿全部用例：**流式只是多了一条观察通道，不是另一套语义**——
聚合结果必须与非流式完全等价，会话日志里不该出现增量事件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from components.agent import AgentLoop
from components.llm import LLMRequest, text_reply, tool_call_reply
from components.llm.deepseek import chunk_from_delta
from components.llm.fake import (
    FakeLLM,
    streamed_text,
    streamed_tool_call,
)
from components.llm.streaming import (
    StreamChunk,
    aggregate_chunks,
    chunks_from_response,
)
from plugins import boot_offline

# ---------------------------------------------------------------------------
# 聚合器（纯函数）
# ---------------------------------------------------------------------------


def test_aggregate_text_chunks():
    response = aggregate_chunks([StreamChunk(text="你"), StreamChunk(text="好"), StreamChunk(finish_reason="stop")])
    assert response.message.content == "你好"
    assert response.finish_reason == "stop"
    assert response.message.tool_calls == []


def test_aggregate_tool_call_fragments():
    """工具参数分片到达：拼完再解析成字典。"""
    chunks = [
        StreamChunk(tool_index=0, tool_id="call_1", tool_name="calculate"),
        StreamChunk(tool_index=0, tool_arguments='{"expr'),
        StreamChunk(tool_index=0, tool_arguments='ession": "1+1"}'),
        StreamChunk(finish_reason="tool_calls"),
    ]
    response = aggregate_chunks(chunks)
    assert response.finish_reason == "tool_calls"
    call = response.message.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "calculate", {"expression": "1+1"})


def test_aggregate_parallel_tool_calls_keep_order():
    """并行工具调用：按 tool_index 分组、保持出现顺序。"""
    chunks = [
        StreamChunk(tool_index=0, tool_id="a", tool_name="f1", tool_arguments="{}"),
        StreamChunk(tool_index=1, tool_id="b", tool_name="f2", tool_arguments="{}"),
        StreamChunk(finish_reason="tool_calls"),
    ]
    calls = aggregate_chunks(chunks).message.tool_calls
    assert [(c.id, c.name) for c in calls] == [("a", "f1"), ("b", "f2")]


def test_aggregate_text_and_tool_calls_together():
    """模型边说话边申请工具（真实行为）。"""
    chunks = [
        StreamChunk(text="我来算一下"),
        StreamChunk(tool_index=0, tool_id="c1", tool_name="calculate", tool_arguments='{"expression": "2+3"}'),
        StreamChunk(finish_reason="tool_calls"),
    ]
    response = aggregate_chunks(chunks)
    assert response.message.content == "我来算一下"
    assert response.message.tool_calls[0].name == "calculate"


def test_aggregate_bad_json_fails_loud():
    with pytest.raises(ValueError) as excinfo:
        aggregate_chunks([
            StreamChunk(tool_index=0, tool_name="f", tool_arguments='{"broken": '),
            StreamChunk(finish_reason="tool_calls"),
        ])
    assert "不是合法 JSON" in str(excinfo.value)


def test_aggregate_missing_name_fails_loud():
    with pytest.raises(ValueError) as excinfo:
        aggregate_chunks([StreamChunk(tool_index=0, tool_arguments="{}")])
    assert "缺少工具名" in str(excinfo.value)


def test_round_trip_response_to_chunks_and_back():
    """一次性响应 → 增量序列 → 聚合：等价（同一份语义两种表示）。"""
    original = tool_call_reply("calculate", {"expression": "9*9"}, content="算一下")
    again = aggregate_chunks(chunks_from_response(original))
    assert again.message.content == original.message.content
    call, call2 = original.message.tool_calls[0], again.message.tool_calls[0]
    assert (call.id, call.name, call.arguments) == (call2.id, call2.name, call2.arguments)
    assert again.finish_reason == original.finish_reason


# ---------------------------------------------------------------------------
# Provider 方言（SSE delta → 中立增量）
# ---------------------------------------------------------------------------


def test_chunk_from_delta_translates_text_and_tool_fragments():
    from types import SimpleNamespace as NS

    text_chunks = chunk_from_delta(NS(content="你好", tool_calls=None))
    assert [c.text for c in text_chunks] == ["你好"]
    assert text_chunks[0].tool_index is None

    tool_chunks = chunk_from_delta(
        NS(content=None, tool_calls=[NS(index=0, id="c1", function=NS(name="f", arguments='{"a'))])
    )
    assert len(tool_chunks) == 1
    chunk = tool_chunks[0]
    assert (chunk.tool_index, chunk.tool_id, chunk.tool_name) == (0, "c1", "f")
    assert chunk.tool_arguments == '{"a'

    tail = chunk_from_delta(NS(content=None, tool_calls=None), finish_reason="stop")
    assert [c.finish_reason for c in tail] == ["stop"]


# ---------------------------------------------------------------------------
# FakeLLM 流式剧本：同一份剧本两种模式
# ---------------------------------------------------------------------------


def test_fake_stream_and_complete_agree():
    request = LLMRequest(messages=[])

    streamed = FakeLLM([streamed_text("逐段到达的句子", piece=3)])
    pieces = [c.text for c in streamed.stream(request)]

    plain = FakeLLM([streamed_text("逐段到达的句子", piece=3)])
    response = plain.complete(request)          # ScriptedStream 被聚合

    assert "".join(pieces) == "逐段到达的句子"
    assert response.message.content == "".join(pieces)


def test_fake_stream_tool_call_script():
    fake = FakeLLM([streamed_tool_call("calculate", {"expression": "1+1"}, piece=4)])
    response = fake.complete(LLMRequest(messages=[]))
    call = response.message.tool_calls[0]
    assert call.name == "calculate" and call.arguments == {"expression": "1+1"}


def test_fake_stream_exhausted_fails_loud():
    fake = FakeLLM([streamed_text("x", piece=1)])
    list(fake.stream(LLMRequest(messages=[])))
    with pytest.raises(AssertionError) as excinfo:
        list(fake.stream(LLMRequest(messages=[])))
    assert "剧本已用完" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 循环：流式 / 非流式等价 + 增量事件 + 取消
# ---------------------------------------------------------------------------


def _loop(provider) -> tuple[AgentLoop, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    loop = AgentLoop(provider, lambda name, args: {"ok": True}, on_event=lambda k, p: events.append((k, p)))
    return loop, events


def test_loop_streaming_matches_non_streaming():
    """同一剧本：走流式的循环结果与走非流式的完全一致（状态/文本/工具调用）。"""
    spec = [streamed_tool_call("calc", {"x": 1}), streamed_text("结果已算出", piece=2)]

    stream_provider = FakeLLM(spec)
    stream_loop, stream_events = _loop(stream_provider)
    stream_result = stream_loop.run("算一下", tools=[])

    plain_provider = FakeLLM(spec)          # 同一份 spec：complete 会先聚合
    plain_loop, _ = _loop(plain_provider)
    plain_result = plain_loop.run("算一下", tools=[])

    assert stream_result.status == plain_result.status == "done"
    assert stream_result.final_text == plain_result.final_text == "结果已算出"
    assert len(stream_result.steps) == len(plain_result.steps) == 2
    # 增量事件只在流式路径出现
    kinds = [kind for kind, _ in stream_events]
    assert "step_delta" in kinds
    assert "step_response" in kinds


def test_loop_emits_step_delta_with_text_pieces():
    provider = FakeLLM([streamed_text("你好世界", piece=2)])
    loop, events = _loop(provider)
    loop.run("打个招呼", tools=[])

    deltas = [payload["text"] for kind, payload in events if kind == "step_delta" and payload["text"]]
    assert "".join(deltas) == "你好世界"
    assert len(deltas) > 1                   # 确实是分片，不是一次给完


def test_loop_falls_back_when_provider_has_no_stream():
    """没有 stream() 的 provider：循环照跑（退回 complete()），不发增量事件。"""

    class PlainOnlyLLM:
        """只实现 complete() 的最小 provider（模拟不支持流式的实现）。"""

        def complete(self, request):
            return text_reply("只有一次性回答")

    loop, events = _loop(PlainOnlyLLM())  # type: ignore[arg-type]
    result = loop.run("问一句", tools=[])
    assert result.final_text == "只有一次性回答"
    assert not [k for k, _ in events if k == "step_delta"]


def test_loop_cancel_during_stream_keeps_partial():
    """流式中途取消：关闭上游、保留已收到的部分、结构化收尾。"""
    provider = FakeLLM([streamed_text("一二三四五六七八", piece=1)])
    loop, events = _loop(provider)

    def stop_after_two() -> bool:
        # 收到两个增量后触发取消（循环在下一个增量处检查）
        return len([k for k, _ in events if k == "step_delta"]) >= 2

    result = loop.run("长回答", tools=[], should_stop=stop_after_two)
    assert result.status == "cancelled"
    assert "stream_cancelled" in [k for k, _ in events]
    # 已经收到的部分被保留下来（不是空回复）
    cancelled_payload = next(p for k, p in events if k == "stream_cancelled")
    assert cancelled_payload["received_chars"] >= 1


# ---------------------------------------------------------------------------
# 会话日志不受流式影响
# ---------------------------------------------------------------------------


def test_session_log_has_no_transient_events(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[streamed_text("逐段到达", piece=2)],
    )
    booted.agent.ask("你好", session_id="s1")
    types = [e.type for e in booted.agent.open("s1").session.events]
    assert "step_delta" not in types and "stream_cancelled" not in types
    # 事实仍在：assistant/message 记的是聚合后的完整文本
    assistant = next(e for e in booted.agent.open("s1").session.events if e.type == "assistant/message")
    assert assistant.data["content"] == "逐段到达"
    booted.close()


def test_llm_stream_event_broadcast_in_full_stack(tmp_path: Path):
    """整装链路：llm 插件把增量广播为 llm/stream（供界面/RPC 订阅）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[text_reply("固定回复")],
    )
    deltas: list = []
    booted.ctx.on("llm/stream", lambda **kw: deltas.append(kw["chunk"]))
    result = booted.agent.ask("你好", session_id="s1")
    assert deltas, "默认 provider 应支持流式（RepeatingLLM.stream）"
    assert "".join(c.text for c in deltas) == result.final_text
    booted.close()


def test_prompt_provider_passes_through_stream(tmp_path: Path):
    """每步渲染包装器必须透传流式（曾漏掉：内层流式被吞，循环白退回一次性）。"""
    from components.prompt.assembler import PromptAssembler
    from components.prompt.provider import RuntimePromptProvider
    from components.prompt.registry import SectionRegistry
    from components.prompt.renderer import PromptRenderer
    from components.prompt.runtime import RuntimeContext
    from components.prompt.section import Section

    registry = SectionRegistry()
    registry.register(Section(name="role", content="你是助手"))
    renderer = PromptRenderer(
        PromptAssembler(registry),
        lambda: RuntimeContext(cwd=".", platform="test", time="2026-01-01T00:00:00"),
    )
    inner = FakeLLM([streamed_text("流式文本", piece=2)])
    provider = RuntimePromptProvider(inner, renderer)
    chunks = list(provider.stream(LLMRequest(messages=[])))
    assert "".join(c.text for c in chunks) == "流式文本"


# ---------------------------------------------------------------------------
# 界面：逐字渲染的元素与帧处理
# ---------------------------------------------------------------------------


def test_index_page_renders_streaming_ui(tmp_path: Path):
    from plugins import WEBUI_SERVICE, webui_plugin

    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    import urllib.request
    # `_05` 起页面拆成三件套：流式逻辑在 app.js，样式在 app.css
    with urllib.request.urlopen(url + "app.js", timeout=8) as response:
        js = response.read().decode("utf-8")
    with urllib.request.urlopen(url + "app.css", timeout=8) as response:
        css = response.read().decode("utf-8")
    assert 'case "step_delta":' in js            # 增量帧处理
    assert "streaming-live" in js and ".cursor{" in css      # 流式气泡与光标
    assert "stream_cancelled" in js              # 取消收尾
    booted.ctx.unload_plugin("webui")
    booted.close()


# ---------------------------------------------------------------------------
# 诚实代理：包装器不能谎报流式能力
# ---------------------------------------------------------------------------


def test_prompt_provider_hides_stream_when_inner_lacks_it():
    """内层没有 stream 时，每步渲染包装器**也不能暴露** stream。

    回归：曾写成"总是有 stream、调用时才报错"，导致循环探测到就当流式用，
    遇到只有 complete() 的 provider 直接炸（实测在环境里炸过一次）。
    """
    from components.prompt.assembler import PromptAssembler
    from components.prompt.provider import RuntimePromptProvider
    from components.prompt.registry import SectionRegistry
    from components.prompt.renderer import PromptRenderer
    from components.prompt.runtime import RuntimeContext

    class PlainOnly:
        def complete(self, request):
            return text_reply("ok")

    registry = SectionRegistry()
    renderer = PromptRenderer(
        PromptAssembler(registry),
        lambda: RuntimeContext(cwd=".", platform="t", time="2026-01-01T00:00:00"),
    )
    provider = RuntimePromptProvider(PlainOnly(), renderer)
    assert not hasattr(provider, "stream"), "内层不支持时不该暴露 stream"


def test_llm_plugin_proxy_hides_stream_when_inner_lacks_it():
    """llm 插件的包装器同理（探测接口不能撒谎）。"""
    from kernel import Context
    from plugins.llm import LlmPlugin

    class PlainOnly:
        def complete(self, request):
            return text_reply("ok")

    ctx = Context()
    ctx.load_plugin(LlmPlugin(PlainOnly()))
    service = ctx.require("llm")
    assert not hasattr(service, "stream")
    assert service.complete(LLMRequest(messages=[])).message.content == "ok"


# ---------------------------------------------------------------------------
# 传输层：真流式必须走 chunked（否则"逐字"会退化成"一次全到"）
# ---------------------------------------------------------------------------


def test_send_endpoint_uses_chunked_transfer(tmp_path: Path):
    """`/send` 用 chunked 传输，且分帧正确（错误分帧会让客户端读挂）。

    回归背景：HTTP/1.0 下响应不带长度也不带 chunked，客户端只能等连接关闭
    才认账——逐字到达会退化成"全部一次到达"（浏览器里看不出流式）。
    修法是 HTTP/1.1 + 显式 chunked + 每帧 flush + 收尾写零长度块。
    本用例从客户端侧验证：头是 chunked，且所有帧都能正常读完（分帧错会卡死）。
    """
    import json as _json
    import urllib.request

    from components.llm.fake import streamed_text
    from plugins import WEBUI_SERVICE, webui_plugin

    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[streamed_text("逐字到达的文本内容", piece=2)],
    )
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    request = urllib.request.Request(
        url + "api/sessions/chunked-probe/send",
        data=_json.dumps({"text": "你好"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.headers.get("Transfer-Encoding") == "chunked"
        frames = [_json.loads(line) for line in response if line.strip()]

    kinds = [f["kind"] for f in frames]
    assert kinds[-1] == "__done", kinds
    deltas = [f for f in frames if f["kind"] == "step_delta"]
    assert len(deltas) > 1                       # 分片到达
    assert "".join(d["payload"]["text"] for d in deltas) == "逐字到达的文本内容"
    booted.ctx.unload_plugin("webui")
    booted.close()
