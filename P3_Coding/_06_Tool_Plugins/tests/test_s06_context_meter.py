"""`_04` 上下文仪表测试：估算、缩放、缓存命中、页面、挂载。

一条纪律：**占比来自估算、总量来自实测**——四类估算按比例缩放到实测总额，
所以"四类之和 == 实测总额"是硬断言（不能因为舍入差一个）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from components.llm.vocabulary import Usage
from plugins import (
    CONTEXT_METER_SERVICE,
    WEBUI_SERVICE,
    boot_offline,
    context_meter_plugin,
    estimate_tokens,
    measure_parts,
    webui_plugin,
)


def _u(text: str, prompt: int, completion: int, *, hit: int = 0, miss: int = 0):
    response = text_reply(text)
    response.usage = Usage(
        prompt, completion,
        prompt_cache_hit_tokens=hit,
        prompt_cache_miss_tokens=miss,
    )
    return response


def _isolated(tmp_path: Path, *, script: list, capacity: int = 65536):
    """装配后用 port=0 重装仪表：每个用例独立端口与读数。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=script, context_window=capacity,
    )
    booted.ctx.unload_plugin("context-meter")
    booted.ctx.load_plugin(context_meter_plugin(capacity=capacity, port=0))
    return booted


def _read(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


# ---------------------------------------------------------------------------
# 估算（纯函数）
# ---------------------------------------------------------------------------


def test_estimate_tokens_prefers_cjk_ratio():
    """中文按 0.6/字、其他按 0.3/字符——同样字数下中文估得更多。"""
    assert estimate_tokens("你好世界") == pytest.approx(2.4)
    assert estimate_tokens("abcd") == pytest.approx(1.2)
    assert estimate_tokens("") == 0


def test_measure_parts_splits_four_categories():
    from components.llm.vocabulary import assistant, system, tool_result, user

    messages = [system("你是助手"), user("你好"), assistant("在的"), tool_result("c1", {"ok": True})]
    tools = []
    parts = measure_parts(messages, tools)
    assert set(parts) == {"消息", "系统工具", "系统提示词", "其他"}
    assert parts["系统提示词"] > 0
    assert parts["消息"] > 0
    assert parts["其他"] > 0


def test_measure_parts_counts_tools_and_arguments():
    from components.llm.vocabulary import ToolCall, ToolSpec, assistant, user

    spec = ToolSpec(name="tool_x", description="描述" * 50, parameters={"type": "object"})
    with_tool = measure_parts([user("hi")], [spec])
    without = measure_parts([user("hi")], [])
    assert with_tool["系统工具"] > without["系统工具"]

    call = ToolCall(id="c1", name="tool_x", arguments={"a": "值" * 30})
    with_args = measure_parts([assistant("", [call])], [])
    no_args = measure_parts([assistant("")], [])
    assert with_args["消息"] > no_args["消息"]


# ---------------------------------------------------------------------------
# 快照：实测优先 + 缩放后四类之和 == 总额
# ---------------------------------------------------------------------------


def test_snapshot_uses_actual_after_response(tmp_path: Path):
    booted = _isolated(tmp_path, script=[_u("回答", 2000, 50)], capacity=10000)
    service = booted.ctx.require(CONTEXT_METER_SERVICE)

    before = _read(service.url + "api/context")
    assert before["used_source"] == "estimate"      # 还没有实测

    booted.agent.ask("问", session_id="s1")
    after = _read(service.url + "api/context")
    assert after["used_source"] == "actual"
    assert after["used"] == 2000                    # 实测优先
    assert after["percent"] == 20.0
    booted.close()


def test_parts_sum_equals_used_after_scaling(tmp_path: Path):
    booted = _isolated(tmp_path, script=[_u("答", 3333, 10)], capacity=10000)
    booted.agent.ask("问一个稍微长一点的问题看看占比", session_id="s1")
    data = _read(booted.ctx.require(CONTEXT_METER_SERVICE).url + "api/context")
    assert data["used"] == 3333
    assert sum(p["tokens"] for p in data["parts"]) == data["used"]   # 余数给了最大项
    assert abs(sum(p["percent"] for p in data["parts"]) - 100.0) < 0.5
    booted.close()


def test_estimate_source_before_any_response(tmp_path: Path):
    booted = _isolated(tmp_path, script=[_u("x", 1, 1)], capacity=65536)
    # 触发一次 request（通过跑一个 turn 但响应前读数不行——改为直接观察请求）
    service = booted.ctx.require(CONTEXT_METER_SERVICE)
    data = _read(service.url + "api/context")
    assert data["used_source"] == "estimate"
    assert data["used"] == 0                        # 还没发过请求
    booted.close()


def test_capacity_zero_or_negative_fails_loud():
    from plugins import ContextMeter

    with pytest.raises(ValueError):
        ContextMeter(capacity=0)
    with pytest.raises(ValueError):
        context_meter_plugin(capacity=-1)


# ---------------------------------------------------------------------------
# 缓存命中：加权平均
# ---------------------------------------------------------------------------


def test_cache_hit_rate_is_weighted_average(tmp_path: Path):
    """命中率 = Σhit / (Σhit + Σmiss)：大请求权重更大（不是各次算术平均）。"""
    booted = _isolated(
        tmp_path,
        script=[
            _u("一", 1000, 10, hit=900, miss=100),    # 这次 90%
            _u("二", 9000, 10, hit=1000, miss=8000),  # 这次 11%（但量很大）
        ],
        capacity=20000,
    )
    booted.agent.ask("第一问", session_id="s1")
    booted.agent.ask("第二问", session_id="s1")
    data = _read(booted.ctx.require(CONTEXT_METER_SERVICE).url + "api/context")
    cache = data["cache"]
    assert cache["hit_tokens"] == 1900 and cache["miss_tokens"] == 8100
    # 算术平均会是 (0.9+0.111)/2 ≈ 0.51；加权平均是 1900/10000 = 0.19
    assert cache["hit_rate"] == pytest.approx(0.19)
    assert cache["calls"] == 2
    booted.close()


def test_cache_rate_none_when_provider_does_not_report(tmp_path: Path):
    """provider 不报缓存（FakeLLM 就不报）时显示"—"，不假装 0%。"""
    booted = _isolated(tmp_path, script=[text_reply("没缓存数据")], capacity=10000)
    booted.agent.ask("问", session_id="s1")
    data = _read(booted.ctx.require(CONTEXT_METER_SERVICE).url + "api/context")
    assert data["cache"]["hit_rate"] is None
    assert data["cache"]["calls"] == 0
    booted.close()


def test_usage_cache_fields_accumulate():
    a = Usage(10, 1, prompt_cache_hit_tokens=5, prompt_cache_miss_tokens=5)
    b = Usage(20, 2, prompt_cache_hit_tokens=3, prompt_cache_miss_tokens=17)
    total = a + b
    assert total.prompt_cache_hit_tokens == 8
    assert total.prompt_cache_miss_tokens == 22
    assert total.cache_reported is True
    assert Usage(1, 1).cache_reported is False


def test_deepseek_extracts_cache_fields_from_response():
    """真实 provider 从 API 响应里带上缓存字段（离线断言，不需要网络）。"""
    from types import SimpleNamespace as NS

    from components.llm.deepseek import response_from_api

    api_response = NS(
        choices=[NS(message=NS(content="ok", tool_calls=None), finish_reason="stop")],
        usage=NS(
            prompt_tokens=1000,
            completion_tokens=20,
            prompt_cache_hit_tokens=800,
            prompt_cache_miss_tokens=200,
        ),
    )
    usage = response_from_api(api_response).usage
    assert usage.prompt_cache_hit_tokens == 800
    assert usage.prompt_cache_miss_tokens == 200


# ---------------------------------------------------------------------------
# 页面与接口
# ---------------------------------------------------------------------------


def test_page_served_and_unknown_route_404(tmp_path: Path):
    booted = _isolated(tmp_path, script=[_u("x", 1, 1)])
    url = booted.ctx.require(CONTEXT_METER_SERVICE).url
    with urllib.request.urlopen(url, timeout=8) as response:
        html = response.read().decode("utf-8")
    assert "上下文容量" in html and "/api/context" in html

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url + "nope", timeout=8)
    assert excinfo.value.code == 404
    booted.close()


def test_api_sends_cors_header(tmp_path: Path):
    booted = _isolated(tmp_path, script=[_u("x", 1, 1)])
    url = booted.ctx.require(CONTEXT_METER_SERVICE).url
    with urllib.request.urlopen(url + "api/context", timeout=8) as response:
        assert response.headers.get("Access-Control-Allow-Origin") == "*"
    booted.close()


def test_endpoint_discoverable_and_webui_has_panel(tmp_path: Path):
    """自报地址 + 主界面有挂载点（ctx-btn / ctx-panel）。"""
    booted = _isolated(tmp_path, script=[_u("x", 1, 1)])
    booted.ctx.load_plugin(webui_plugin(port=0))
    webui_url = booted.ctx.require(WEBUI_SERVICE).url

    ledger = _read(webui_url + "api/ledger")
    assert "context-meter" in ledger["endpoints"]
    assert ledger["endpoints"]["context-meter"].startswith("http://127.0.0.1:")

    with urllib.request.urlopen(webui_url, timeout=8) as response:
        html = response.read().decode("utf-8")
    # 标记在 index.html，挂载逻辑在 app.js（`_05` 起拆成三件套）
    # `_05` 收尾后：壳不再写死面板元素，改成一个通用容器 + 面板自述。
    # 断言换成"通用挂载点 + 这个插件自述的面板在列表里"——比原来更严：
    # 原来只查字符串在文件里（写死也算过），现在查的是**真的被发布了**。
    assert "plugin-panel" in html and "panel-btns" in html
    assert "ctx-panel" not in html and "ctx-btn" not in html   # 旧写法已清
    labels = {p["service"]: p["label"] for p in ledger["panels"]}
    assert "context-meter" in labels
    assert "上下文" in labels["context-meter"]
    booted.close()


def test_unload_stops_page_and_releases_port(tmp_path: Path):
    import socket

    booted = _isolated(tmp_path, script=[_u("x", 1, 1)])
    service = booted.ctx.require(CONTEXT_METER_SERVICE)
    port = service.port

    booted.ctx.unload_plugin("context-meter")

    with socket.socket() as probe:
        probe.settimeout(0.5)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    assert CONTEXT_METER_SERVICE not in booted.ctx.services
    booted.close()


def test_serve_false_has_no_page(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[_u("x", 1, 1)], context_window=5000)
    booted.ctx.unload_plugin("context-meter")
    booted.ctx.load_plugin(context_meter_plugin(capacity=5000, serve=False))
    service = booted.ctx.require(CONTEXT_METER_SERVICE)
    assert service.port == 0 and service.capacity == 5000
    booted.close()


def test_meter_reads_after_tool_calls(tmp_path: Path):
    """带工具往返的请求也要被测量（工具结果算进"消息"）。"""
    booted = _isolated(
        tmp_path,
        script=[tool_call_reply("calculate", {"expression": "1+1"}), _u("答", 1500, 20)],
        capacity=10000,
    )
    booted.agent.ask("算一下", session_id="s1")
    data = _read(booted.ctx.require(CONTEXT_METER_SERVICE).url + "api/context")
    assert data["used"] == 1500
    assert sum(p["tokens"] for p in data["parts"]) == 1500
    booted.close()


def test_estimate_reflects_message_growth(tmp_path: Path):
    """同一会话多轮后，估算总量应增长（消息越攒越多）。"""
    booted = _isolated(tmp_path, script=[_u("一", 100, 10), _u("二", 200, 10)], capacity=65536)
    service = booted.ctx.require(CONTEXT_METER_SERVICE)

    booted.agent.ask("第一问", session_id="s1")   # 触发 request，估算入账
    first = _read(service.url + "api/context")
    booted.agent.ask("第二问" * 20, session_id="s1")
    second = _read(service.url + "api/context")
    # 估算总量（used_source=actual 时看不到估算，但 parts 会随消息增长）
    assert second["parts"][0]["tokens"] >= first["parts"][0]["tokens"]
    booted.close()


# ---------------------------------------------------------------------------
# 缓存友好性：默认提示词必须跨 step 稳定（回归：曾引用秒级 {{time}}）
# ---------------------------------------------------------------------------


def test_default_prompt_is_stable_across_seconds():
    """默认提示词不含"每步都变"的变量——否则前缀缓存永远命中不了。

    回归背景：默认 env 小节曾引用 `{{time}}`（精确到秒），而系统提示词是请求的
    第一条消息——它一变，前缀缓存就彻底失效。实测（真实 API，同一请求发三次）：

        稳定前缀：  prompt=564  hit=0/384/384
        秒级时间：  prompt=564  hit=0/0/0

    所以默认提示词改用 `{{date}}`（一天内不变）；`{{time}}` 仍可用，
    但属于"要精度就放弃缓存"的自选动作。
    """
    from datetime import datetime, timedelta

    from components.prompt import capture_trace, collect_runtime_context, default_assembler

    t1 = datetime(2026, 10, 14, 10, 0, 1).astimezone()
    t2 = t1 + timedelta(seconds=17)          # 同一分钟内的不同秒

    a = capture_trace(default_assembler(), collect_runtime_context(".", now=lambda: t1).variables())
    b = capture_trace(default_assembler(), collect_runtime_context(".", now=lambda: t2).variables())
    assert a.text == b.text, "默认提示词在同一分钟内应逐字节相同（缓存友好）"

    referenced = {name for section in a.sections for name in section.referenced}
    assert "time" not in referenced
    assert {"cwd", "platform", "date"} <= referenced


def test_explicit_time_variable_still_available():
    """`{{time}}` 没被删掉：显式引用它的 section 仍然每步重渲染（自选代价）。"""
    from datetime import datetime

    from components.prompt import PromptAssembler, Section, SectionRegistry
    from components.prompt.defaults import default_sections
    from components.prompt.runtime import collect_runtime_context

    registry = SectionRegistry()
    for section in default_sections():
        registry.register(section)
    registry.register(Section(name="clock", content="当前时间 {{time}}。"))
    assembler = PromptAssembler(registry)

    moment = datetime(2026, 10, 14, 10, 0, 1).astimezone()
    text = assembler.assemble(collect_runtime_context(".", now=lambda: moment).variables())
    assert "当前时间 2026-10-14T10:00:01" in text


def test_runtime_context_derives_date_from_time():
    """只给 time 的旧调用点也要能工作（date 自动推导，不让 {{date}} 渲染成空）。"""
    from components.prompt.runtime import RuntimeContext

    ctx = RuntimeContext(cwd=".", platform="t", time="2026-01-02T03:04:05")
    assert ctx.date == "2026-01-02"
    assert ctx.variables()["date"] == "2026-01-02"
