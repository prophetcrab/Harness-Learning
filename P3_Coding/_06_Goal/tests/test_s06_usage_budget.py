"""`_03` 用量与预算测试：累计正确、超限结构化收尾、账本、接口、页面。

一条纪律贯穿：**超预算是"结果"不是"异常"**——turn 以 `budget_exceeded` 收尾，
部分文本保留，用量如实带回；调用方据此决定下一步（提醒用户、降级、停止）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from components.llm.fake import FakeLLM
from components.llm.streaming import StreamChunk, aggregate_chunks, chunks_from_response
from components.llm.vocabulary import Usage
from plugins import (
    TOKEN_USAGE_SERVICE,
    TOOLS_SERVICE,
    boot_offline,
    token_usage_plugin,
)


def _boot_isolated(tmp_path: Path, *, script: list, **kwargs):
    """装配后用 port=0 重装 token-usage：每个用例得到独立账本与端口。

    默认端口 8767 是"固定入口"，多个用例同时起会共用同一个服务实例
    （账本互相污染，实测 calls 从 4 变成 6）；测试要的是隔离。
    """
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=script, **kwargs)
    booted.ctx.unload_plugin("token-usage")
    booted.ctx.load_plugin(token_usage_plugin(port=0))
    return booted


def _u(text: str, prompt: int, completion: int):
    """一条带用量的一次性回复。"""
    response = text_reply(text)
    response.usage = Usage(prompt, completion)
    return response


def _tc(name: str, args: dict, prompt: int, completion: int, cid: str = "c1"):
    """一条带用量的工具调用回复。"""
    response = tool_call_reply(name, args, call_id=cid)
    response.usage = Usage(prompt, completion)
    return response


# ---------------------------------------------------------------------------
# 构件：Usage 累加与流式用量
# ---------------------------------------------------------------------------


def test_usage_accumulates_and_totals():
    total = Usage(10, 5) + Usage(100, 50)
    assert (total.prompt_tokens, total.completion_tokens) == (110, 55)
    assert total.total_tokens == 165
    assert Usage().total_tokens == 0


def test_streaming_carries_usage_from_final_chunk():
    """流式路径下用量在末帧：聚合后必须带回（否则账本永远为空）。"""
    chunks = [
        StreamChunk(text="你"),
        StreamChunk(text="好"),
        StreamChunk(finish_reason="stop", usage=Usage(30, 7)),
    ]
    response = aggregate_chunks(chunks)
    assert response.usage is not None and response.usage.total_tokens == 37


def test_chunks_from_response_carries_usage():
    """一次性响应"播放"成增量时，usage 挂在末帧，聚合回来不丢。"""
    original = _u("文本", 11, 22)
    again = aggregate_chunks(chunks_from_response(original))
    assert again.usage is not None and again.usage.total_tokens == 33


def test_streaming_fake_records_usage():
    """FakeLLM 的流式剧本也能带用量（与一次性路径同一份语义）。"""
    from components.llm.fake import streamed_text

    streamed = streamed_text("带用量的流式回复", piece=2)
    # 手动把 usage 挂到末帧（剧本积木本身不管用量，这里模拟 provider 行为）
    chunks = list(streamed.chunks)
    chunks[-1] = StreamChunk(finish_reason="stop", usage=Usage(40, 10))
    response = aggregate_chunks(chunks)
    assert response.usage.total_tokens == 50


# ---------------------------------------------------------------------------
# 循环：turn 用量累计
# ---------------------------------------------------------------------------


def test_turn_usage_sums_all_steps(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[
            _tc("calculate", {"expression": "2+3"}, 100, 20),
            _u("算好了", 150, 30),
        ],
    )
    result = booted.agent.ask("帮我算 2+3", session_id="s1")
    assert result.status == "done"
    assert result.usage.prompt_tokens == 250
    assert result.usage.completion_tokens == 50
    assert result.usage.total_tokens == 300
    booted.close()


def test_usage_missing_from_provider_is_zero(tmp_path: Path):
    """provider 不报用量时按 0 计（不能崩、不能虚报）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[text_reply("没有用量字段")],          # 不带 usage
    )
    result = booted.agent.ask("问", session_id="s1")
    assert result.status == "done"
    assert result.usage.total_tokens == 0
    booted.close()


def test_step_usage_event_emitted_per_call(tmp_path: Path):
    events: list[tuple[str, dict]] = []
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[_tc("calculate", {"expression": "1+1"}, 10, 5), _u("好", 20, 8)],
    )
    booted.ctx.on("loop/event", lambda **kw: events.append((kw["kind"], kw["payload"])))
    booted.agent.ask("算", session_id="s1")
    usages = [p for k, p in events if k == "step_usage"]
    assert len(usages) == 2
    assert usages[0]["usage"].total_tokens == 15
    assert usages[1]["turn_total"] == 43          # 15 + 28
    booted.close()


# ---------------------------------------------------------------------------
# 预算：超限是"结果"，不是异常
# ---------------------------------------------------------------------------


def test_budget_exceeded_single_step(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws", token_budget=100,
        script=[_u("很长的回答", 150, 30)],
    )
    result = booted.agent.ask("问一句", session_id="s1")
    assert result.status == "budget_exceeded"
    assert result.usage.total_tokens == 180
    assert result.final_text == "很长的回答"       # 已生成的部分保留
    booted.close()


def test_budget_exceeded_accumulates_across_steps(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws", token_budget=250,
        script=[
            _tc("calculate", {"expression": "2+3"}, 100, 20),   # 120
            _tc("calculate", {"expression": "5+5"}, 100, 20),   # 240（仍在预算内）
            _u("两个都算完了", 100, 20),                          # 360 → 超
        ],
    )
    result = booted.agent.ask("算两次", session_id="s1")
    assert result.status == "budget_exceeded"
    assert result.usage.total_tokens == 360
    assert len(result.steps) == 3
    booted.close()


def test_budget_not_exceeded_stays_done(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws", token_budget=1000,
        script=[_u("正常回答", 100, 20)],
    )
    result = booted.agent.ask("问", session_id="s1")
    assert result.status == "done" and result.usage.total_tokens == 120
    booted.close()


def test_budget_exactly_at_limit_is_not_exceeded(tmp_path: Path):
    """边界：恰好等于预算是允许的（"超过"才拦）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws", token_budget=120,
        script=[_u("刚好", 100, 20)],
    )
    result = booted.agent.ask("问", session_id="s1")
    assert result.status == "done"
    booted.close()


def test_budget_none_means_unlimited(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[
            _tc("calculate", {"expression": "1+1"}, 999_999, 1),   # 巨额用量
            _u("还是答完了", 1, 1),                                  # 没有预算 → 不拦
        ],
    )
    result = booted.agent.ask("问", session_id="s1")
    assert result.status == "done"                       # 没有预算就一直跑
    assert result.usage.total_tokens == 1_000_002
    booted.close()


def test_budget_must_be_positive(tmp_path: Path):
    from components.agent import AgentLoop

    with pytest.raises(ValueError):
        AgentLoop(FakeLLM([text_reply("x")]), lambda n, a: {}, token_budget=0)


def test_budget_events_persisted_in_log(tmp_path: Path):
    """用量与超限都是**事实**：要进会话日志（与 step_delta 那种"过程"不同）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws", token_budget=100,
        script=[_u("超了", 150, 30)],
    )
    booted.agent.ask("问", session_id="s1")
    events = booted.agent.open("s1").session.events
    types = [e.type for e in events]
    assert "usage/step" in types
    assert "budget/exceeded" in types
    usage_event = next(e for e in events if e.type == "usage/step")
    assert usage_event.data["turn_total"] == 180
    exceeded = next(e for e in events if e.type == "budget/exceeded")
    assert exceeded.data["used"] == 180 and exceeded.data["budget"] == 100
    booted.close()


# ---------------------------------------------------------------------------
# token-usage 插件：账本 / 工具 / 接口 / 页面
# ---------------------------------------------------------------------------


def test_ledger_accumulates_by_turn(tmp_path: Path):
    booted = _boot_isolated(tmp_path, script=[
        # turn 1：工具调用（120）+ 收尾（180）= 300
        _tc("calculate", {"expression": "1+1"}, 100, 20),
        _u("一算好了", 150, 30),
        # turn 2：工具调用（240）+ 收尾（60）= 300
        _tc("calculate", {"expression": "2+2"}, 200, 40, cid="c2"),
        _u("二也算好了", 50, 10),
    ])
    booted.agent.ask("第一问", session_id="s1")
    booted.agent.ask("第二问", session_id="s1")

    service = booted.ctx.require(TOKEN_USAGE_SERVICE)
    with urllib.request.urlopen(service.url + "api/usage", timeout=8) as response:
        data = json.load(response)
    assert data["calls"] == 4
    assert data["totals"] == {
        "prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600
    }
    assert [(t["session"], t["turn"], t["total_tokens"]) for t in data["turns"]] == [
        ("s1", 1, 300),
        ("s1", 2, 300),
    ]
    booted.close()


def test_usage_api_sends_cors_header(tmp_path: Path):
    """主界面从别的端口读这个 JSON，必须有 CORS 头。"""
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[_u("x", 1, 1)])
    service = booted.ctx.require(TOKEN_USAGE_SERVICE)
    with urllib.request.urlopen(service.url + "api/usage", timeout=8) as response:
        assert response.headers.get("Access-Control-Allow-Origin") == "*"
        assert response.headers.get("Cache-Control") == "no-store"
    booted.close()


def test_usage_page_served(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[_u("x", 1, 1)])
    service = booted.ctx.require(TOKEN_USAGE_SERVICE)
    with urllib.request.urlopen(service.url, timeout=8) as response:
        html = response.read().decode("utf-8")
    assert "Token 用量" in html and "/api/usage" in html
    booted.close()


def test_usage_unknown_route_404(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[_u("x", 1, 1)])
    service = booted.ctx.require(TOKEN_USAGE_SERVICE)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(service.url + "nope", timeout=8)
    assert excinfo.value.code == 404
    booted.close()


def test_usage_stats_tool_readable_by_model(tmp_path: Path):
    """模型可自查用量（预算敏感场景需要它知道自己花了多少）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[_tc("calculate", {"expression": "1+1"}, 10, 5), _u("好", 20, 8)],
    )
    tool = booted.ctx.require(TOOLS_SERVICE).resolve("usage_stats")
    assert tool is not None
    assert tool.func()["totals"]["total_tokens"] == 0    # 还没对话
    booted.agent.ask("问", session_id="s1")
    assert tool.func()["totals"]["total_tokens"] == 43   # 15 + 28
    booted.close()


def test_endpoint_discoverable_via_ledger(tmp_path: Path):
    """`/api/ledger` 的 endpoints 里能看到 token-usage 地址（主界面据此挂载）。"""
    from plugins import WEBUI_SERVICE, webui_plugin

    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[_u("x", 1, 1)])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    with urllib.request.urlopen(url + "api/ledger", timeout=8) as response:
        ledger = json.load(response)
    assert "token-usage" in ledger["endpoints"]
    assert ledger["endpoints"]["token-usage"].startswith("http://127.0.0.1:")
    booted.close()


def test_plugin_unload_stops_page_and_removes_tool(tmp_path: Path):
    """卸载即停：页面端口释放、工具注销（注册即 effect）。"""
    import socket

    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[_u("x", 1, 1)])
    # 换到系统分配的端口，避免与并发的其它用例争抢默认的 8767
    booted.ctx.unload_plugin("token-usage")
    booted.ctx.load_plugin(token_usage_plugin(port=0))
    service = booted.ctx.require(TOKEN_USAGE_SERVICE)
    port = service.port
    assert booted.ctx.require(TOOLS_SERVICE).resolve("usage_stats") is not None

    booted.ctx.unload_plugin("token-usage")

    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0     # 端口已释放
    assert TOKEN_USAGE_SERVICE not in booted.ctx.services
    assert booted.ctx.require(TOOLS_SERVICE).resolve("usage_stats") is None
    booted.close()


def test_serve_false_records_without_page(tmp_path: Path):
    """serve=False：只记账不起页面（测试与嵌入场景用）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[_u("x", 5, 5)], self_tools=False,
    )
    # 替换成不起页面的实例
    booted.ctx.unload_plugin("token-usage")
    booted.ctx.load_plugin(token_usage_plugin(serve=False))
    booted.agent.ask("问", session_id="s1")
    assert booted.ctx.require(TOKEN_USAGE_SERVICE).port == 0     # 没有真实监听
    booted.close()


def test_usage_survives_multiple_plugin_reloads(tmp_path: Path):
    """账本随插件生命周期：卸载即清空（内存态，与"每次启动重新装配"一致）。"""
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[_u("x", 9, 9)])
    booted.agent.ask("问", session_id="s1")
    assert booted.ctx.require(TOKEN_USAGE_SERVICE) is not None

    booted.ctx.unload_plugin("token-usage")
    booted.ctx.load_plugin(token_usage_plugin(port=0))
    fresh = booted.ctx.require(TOKEN_USAGE_SERVICE)
    with urllib.request.urlopen(fresh.url + "api/usage", timeout=8) as response:
        assert json.load(response)["calls"] == 0        # 新账本，从零开始
    booted.close()


# ---------------------------------------------------------------------------
# 真实 provider 的流式用量（回归：默认不报，必须显式开启）
# ---------------------------------------------------------------------------


def test_deepseek_stream_requests_usage_inclusion():
    """DeepSeek 流式必须显式要求上报用量（OpenAI 兼容的 include_usage）。

    回归背景：**流式默认不返回 usage**（只有一次性调用才有），不开启这个选项时
    用量账本永远是 0——实测在真实 API 上踩到过。这里钉住请求形状（离线断言，
    不需要网络）。
    """
    from components.llm.deepseek import DeepSeekProvider
    from components.llm.provider import LLMRequest as Req

    captured: dict = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return iter([])              # 空流：只看请求参数

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    provider._client = FakeClient()
    provider._model = "deepseek-chat"

    list(provider.stream(Req(messages=[])))
    assert captured.get("stream") is True
    assert captured.get("stream_options") == {"include_usage": True}


def test_chunk_usage_from_frame_reads_frame_level_usage():
    """用量挂在**帧**上（不是 delta 上），且该帧的 choices 通常为空。"""
    from types import SimpleNamespace as NS

    from components.llm.deepseek import chunk_usage_from_frame

    frame = NS(choices=[], usage=NS(prompt_tokens=34, completion_tokens=47))
    usage = chunk_usage_from_frame(frame)
    assert usage is not None and usage.total_tokens == 81
    assert chunk_usage_from_frame(NS(choices=[], usage=None)) is None


def test_stream_usage_flows_into_turn_result(tmp_path: Path):
    """流式路径的用量也要进 turn 结果（否则账本对流式是瞎的）。"""
    from components.llm.fake import streamed_text
    from components.llm.streaming import StreamChunk

    streamed = streamed_text("带用量的流式回答", piece=3)
    chunks = list(streamed.chunks)
    chunks[-1] = StreamChunk(finish_reason="stop", usage=Usage(60, 20))

    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[type(streamed)(chunks=tuple(chunks))],
    )
    result = booted.agent.ask("问", session_id="s1")
    assert result.status == "done"
    assert result.usage.total_tokens == 80
    booted.close()


def test_ledger_separates_sessions_with_same_turn_numbers(tmp_path: Path):
    """回归：turn 号是**每会话独立编号**的，账本不能把不同会话的同号 turn 并在一起。

    实测踩到：会话 A 与 B 各有 turn 1，只按 turn 聚合会显示成"turn 1 有 4 次调用"。
    """
    booted = _boot_isolated(tmp_path, script=[_u("A1", 100, 10), _u("B1", 200, 20), _u("A2", 50, 5)])
    booted.agent.ask("A 第一问", session_id="alpha")
    booted.agent.ask("B 第一问", session_id="beta")
    booted.agent.ask("A 第二问", session_id="alpha")

    service = booted.ctx.require(TOKEN_USAGE_SERVICE)
    with urllib.request.urlopen(service.url + "api/usage", timeout=8) as response:
        data = json.load(response)
    rows = [(t["session"], t["turn"], t["total_tokens"]) for t in data["turns"]]
    assert rows == [("alpha", 1, 110), ("alpha", 2, 55), ("beta", 1, 220)]
    assert data["sessions"] == ["alpha", "beta"]
    booted.close()
