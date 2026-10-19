"""M2：LLM 三个子插件验收（provider / retry / tokens-usages）。

按用户的要求钉住：

    · provider 由三个子插件组成，**不要求齐全**；缺了哪个由 llm.status() 报告；
    · llm-provider：方言翻译 + 可定制参数 + 连接错误分类；
    · llm-retry：只重试可恢复错误（凭据/请求错误绝不重试）；
    · llm-tokens-usages：总量 / 日（按用户）/ 本对话 三个维度 + 缓存命中率。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    Finish,
    LLMAuthError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    Message,
    TextDelta,
    assemble_text,
    build_payload,
    llm_plugin,
    parse_stream,
)
from plugins.capability.llm.fake import fake_llm_plugin
from plugins.capability.llm.provider import llm_provider_plugin
from plugins.capability.llm.retry import llm_retry_plugin
from plugins.capability.llm.tokens_usages import llm_tokens_usages_plugin


def messages(text: str = "你好") -> tuple[Message, ...]:
    return (Message(role="user", content=text),)


def boot_llm(*, retry: bool = False, usage: bool = False, **llm_config) -> Context:
    llm_config.setdefault("default_provider", "fake")
    llm_config.setdefault("default_model", "fake-1")
    ctx = Context()
    ctx.load_plugin(llm_plugin(**llm_config))
    if retry:
        ctx.load_plugin(llm_retry_plugin(**({"max_attempts": 3} if retry is True else retry)))
    if usage:
        ctx.load_plugin(llm_tokens_usages_plugin())
    return ctx


# ---------------------------------------------------------------------------
# 子插件组成：不要求齐全，缺了要报告
# ---------------------------------------------------------------------------


def test_llm_reports_which_sub_plugins_are_missing():
    ctx = boot_llm()
    llm = ctx.require(LLM_SERVICE)
    assert llm.sub_plugins() == {
        "llm-provider": False,
        "llm-retry": False,
        "llm-tokens-usages": False,
    }
    assert set(llm.missing_sub_plugins()) == {
        "llm-provider",
        "llm-retry",
        "llm-tokens-usages",
    }


def test_llm_works_without_any_sub_plugin():
    """主干不依赖子插件：没有 provider 路由时，自己的能力（校验/默认值）照常。"""
    ctx = boot_llm()
    llm = ctx.require(LLM_SERVICE)
    assert llm.default_route == ("fake", "fake-1")
    assert llm.list_providers() == ()
    with pytest.raises(Exception, match="没有名为"):
        list(llm.stream(llm.request(messages())))


def test_llm_reports_present_sub_plugins():
    ctx = boot_llm(retry=True, usage=True)
    ctx.load_plugin(fake_llm_plugin())
    llm = ctx.require(LLM_SERVICE)
    assert llm.sub_plugins()["llm-retry"] is True
    assert llm.sub_plugins()["llm-tokens-usages"] is True
    # 假适配器不是 llm-provider——按插件名判断，不冒充
    assert llm.sub_plugins()["llm-provider"] is False


def test_provider_plugin_counts_as_sub_plugin():
    ctx = boot_llm()
    ctx.load_plugin(
        llm_provider_plugin(
            route="real",
            base_url="http://example.invalid/chat",
            models=[{"id": "m1", "context_window": 1000}],
            api_key="k",
            transport=lambda *a, **k: iter(()),
        )
    )
    llm = ctx.require(LLM_SERVICE)
    assert llm.sub_plugins()["llm-provider"] is True
    assert "llm-provider" not in llm.missing_sub_plugins()
    assert llm.status()["routes"] == ["real"]


# ---------------------------------------------------------------------------
# llm-provider：方言 + 参数
# ---------------------------------------------------------------------------


def sse(*events: Mapping[str, Any], done: bool = True) -> list[str]:
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}" for event in events]
    if done:
        lines.append("data: [DONE]")
    return lines


def delta(text: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": text}}]}


def finish_event(reason: str = "stop", usage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"choices": [{"delta": {}, "finish_reason": reason}]}
    if usage is not None:
        event["usage"] = usage
    return event


def test_wire_build_payload_merges_params_in_order():
    from plugins.base.llm import LLMRequest

    request = LLMRequest(
        provider="p",
        model="m",
        messages=messages(),
        max_tokens=100,
        temperature=0.3,
        extra={"top_k": 5},
    )
    payload = build_payload(request, default_params={"top_p": 0.9, "top_k": 1})
    assert payload["top_p"] == 0.9
    assert payload["top_k"] == 5           # request.extra 覆盖 default_params
    assert payload["max_tokens"] == 100
    assert payload["stream"] is True
    assert payload["messages"] == [{"role": "user", "content": "你好"}]


def test_wire_parse_stream_text_finish_and_usage():
    lines = sse(
        delta("你"),
        delta("好"),
        finish_event(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 4},
            }
        ),
    )
    chunks = list(parse_stream(lines))
    assert assemble_text(chunks) == "你好"
    finish = chunks[-1]
    assert isinstance(finish, Finish) and finish.reason == "stop"
    assert finish.usage is not None
    assert finish.usage.total_tokens == 12
    assert finish.usage.cached_input_tokens == 4
    assert finish.usage.cache_hit_rate == 0.4


def test_wire_parse_stream_reads_deepseek_cache_field():
    lines = sse(
        delta("x"),
        finish_event(usage={"prompt_tokens": 20, "completion_tokens": 1, "prompt_cache_hit_tokens": 5}),
    )
    finish = list(parse_stream(lines))[-1]
    assert finish.usage is not None and finish.usage.cached_input_tokens == 5


def test_wire_parse_stream_requires_exactly_one_finish():
    """没有 finish_reason / 没有 [DONE] 也要补一个 Finish（契约：恰好一个）。"""
    chunks = list(parse_stream([f"data: {json.dumps(delta('半句'))}"]))
    assert len([c for c in chunks if isinstance(c, Finish)]) == 1
    assert assemble_text(chunks) == "半句"


def test_wire_parse_stream_raises_on_error_event():
    from plugins.base.llm import LLMError

    lines = [f"data: {json.dumps({'error': {'code': 'bad_request', 'message': '坏请求'}})}"]
    with pytest.raises(LLMError, match="bad_request"):
        list(parse_stream(lines))


def test_provider_sends_configured_params_and_headers():
    captured: dict[str, Any] = {}

    def transport(url, headers, payload, timeout) -> Iterable[str]:
        captured.update(url=url, headers=dict(headers), payload=dict(payload), timeout=timeout)
        return sse(delta("ok"), finish_event())

    ctx = boot_llm()
    ctx.load_plugin(
        llm_provider_plugin(
            route="deepseek",
            base_url="https://api.example/chat",
            api_key_env="MY_KEY",   # 环境里没有 → 应在调用时报缺凭据
            models=[{"id": "ds-1", "context_window": 128000, "max_output_tokens": 4096}],
            default_params={"top_p": 0.8},
            transport=transport,
        )
    )
    llm = ctx.require(LLM_SERVICE)
    with pytest.raises(LLMAuthError, match="MY_KEY"):
        list(llm.stream(llm.request(messages(), provider="deepseek", model="ds-1")))

    # 显式给 api_key 之后能走通
    ctx.load_plugin(
        llm_provider_plugin(
            route="deepseek2",
            base_url="https://api.example/chat",
            api_key="secret",
            models=[{"id": "ds-1", "context_window": 128000}],
            default_params={"top_p": 0.8},
            transport=transport,
        )
    )
    chunks = list(llm.stream(llm.request(messages(), provider="deepseek2", model="ds-1")))
    assert assemble_text(chunks) == "ok"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"]["top_p"] == 0.8
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert captured["url"] == "https://api.example/chat"


def test_provider_http_errors_map_to_recoverable_codes():
    import urllib.error

    import plugins.capability.llm.provider as module

    def make_error(status: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "http://x", status, "err", hdrs=None, fp=__import__("io").BytesIO(b"detail")
        )

    for status, error_type in (
        (401, LLMAuthError),
        (429, LLMRateLimitError),
        (500, LLMServerError),
        (400, LLMConnectionError),
    ):
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, _e=make_error(status), **k: (_ for _ in ()).throw(_e)
        try:
            with pytest.raises(error_type):
                list(module.http_transport("http://x", {}, {}, 1.0))
        finally:
            urllib.request.urlopen = original


# ---------------------------------------------------------------------------
# llm-retry
# ---------------------------------------------------------------------------


class FlakyProvider:
    """前 N 次抛错，之后成功（测试重试）。"""

    def __init__(self, failures: int, error: BaseException | None = None) -> None:
        self._failures = failures
        self._error = error or LLMConnectionError("断线")
        self.calls = 0

    def models(self):
        from plugins.capability.llm.fake import fake_model

        return {info.model: info for info in [fake_model("m", provider="flaky")]}

    def stream(self, request):
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error
        yield TextDelta("重试成功")
        yield Finish(reason="stop")


def with_flaky(failures: int, *, error=None, **retry_config):
    ctx = boot_llm(retry=retry_config, default_provider="flaky", default_model="m")
    provider = FlakyProvider(failures, error)
    ctx.require(LLM_SERVICE).register("flaky", provider)
    return ctx, provider


def test_retry_recovers_from_transient_errors():
    ctx, provider = with_flaky(2, max_attempts=3)
    llm = ctx.require(LLM_SERVICE)
    chunks = list(llm.stream(llm.request(messages())))
    assert assemble_text(chunks) == "重试成功"
    assert provider.calls == 3


def test_retry_gives_up_after_max_attempts():
    ctx, provider = with_flaky(10, max_attempts=2)
    llm = ctx.require(LLM_SERVICE)
    with pytest.raises(LLMConnectionError):
        list(llm.stream(llm.request(messages())))
    assert provider.calls == 2


@pytest.mark.parametrize("error", [LLMAuthError("坏 key"), LLMTimeoutError("超时")])
def test_retry_policy_by_error_class(error):
    ctx, provider = with_flaky(10, error=error, max_attempts=3)
    llm = ctx.require(LLM_SERVICE)
    if isinstance(error, LLMAuthError):
        with pytest.raises(LLMAuthError):
            list(llm.stream(llm.request(messages())))
        assert provider.calls == 1          # 凭据错误：不重试
    else:
        with pytest.raises(LLMTimeoutError):
            list(llm.stream(llm.request(messages())))
        assert provider.calls == 3          # 超时：重试到上限


def test_retry_does_nothing_when_plugin_absent():
    ctx = boot_llm(default_provider="flaky", default_model="m")
    provider = FlakyProvider(1)
    ctx.require(LLM_SERVICE).register("flaky", provider)
    with pytest.raises(LLMConnectionError):
        list(ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages())))
    assert provider.calls == 1


def test_retry_handles_error_finish_chunks():
    class ErrorFinishProvider:
        def __init__(self):
            self.calls = 0

        def models(self):
            from plugins.capability.llm.fake import fake_model

            return {info.model: info for info in [fake_model("m", provider="err")]}

        def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield Finish(reason="error", error="上游炸了", error_code="LLM_SERVER_ERROR")
                return
            yield TextDelta("第二次成功")
            yield Finish(reason="stop")

    ctx = boot_llm(retry={"max_attempts": 2}, default_provider="err", default_model="m")
    provider = ErrorFinishProvider()
    ctx.require(LLM_SERVICE).register("err", provider)
    chunks = list(ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages())))
    assert assemble_text(chunks) == "第二次成功"
    assert provider.calls == 2


# ---------------------------------------------------------------------------
# llm-tokens-usages
# ---------------------------------------------------------------------------


def usage_ctx() -> tuple[Context, Any]:
    ctx = boot_llm(usage=True)
    ctx.load_plugin(fake_llm_plugin(replies=["回复"], cached_input_tokens=40))
    return ctx, ctx.require("llm-tokens-usages")


def ask(llm, session_id: str | None, user_id: str | None, text: str = "你好世界"):
    request = llm.request(messages(text), session_id=session_id, user_id=user_id)
    return list(llm.stream(request))


def test_usage_totals_and_session_bucket():
    ctx, store = usage_ctx()
    llm = ctx.require(LLM_SERVICE)
    ask(llm, "s1", "u1")
    ask(llm, "s1", "u1")
    ask(llm, "s2", "u2")

    snap = store.snapshot()
    assert snap["totals"]["calls"] == 3
    assert snap["totals"]["total_tokens"] > 0
    assert snap["sessions"]["s1"]["calls"] == 2
    assert snap["sessions"]["s2"]["calls"] == 1
    assert snap["sessions"]["s1"]["total_tokens"] == snap["sessions"]["s1"]["total_tokens"]


def test_usage_today_is_per_user():
    """日维度按应用用户独立管理（用户的要求）。"""
    ctx, store = usage_ctx()
    llm = ctx.require(LLM_SERVICE)
    ask(llm, "s1", "alice")
    ask(llm, "s2", "alice")
    ask(llm, "s3", "bob")

    snap = store.snapshot()
    assert snap["today_by_user"]["alice"]["calls"] == 2
    assert snap["today_by_user"]["bob"]["calls"] == 1
    assert snap["today_by_user"]["alice"]["total_tokens"] > snap["today_by_user"]["bob"]["total_tokens"]


def test_usage_anonymous_bucket():
    ctx, store = usage_ctx()
    ask(ctx.require(LLM_SERVICE), None, None)
    snap = store.snapshot()
    assert "anonymous" in snap["today_by_user"]
    assert snap["sessions"] == {}          # 没有 session_id：不计入会话桶


def test_usage_cache_hit_rate_per_dimension():
    ctx, store = usage_ctx()
    llm = ctx.require(LLM_SERVICE)
    ask(llm, "s1", "u1", text="你好世界" * 40)  # 输入够长，命中率不会被钳到 1.0

    snap = store.snapshot()
    total = snap["totals"]
    assert total["cached_input_tokens"] == 40
    expected = min(1.0, 40 / total["input_tokens"])
    assert total["cache_hit_rate"] == pytest.approx(expected, rel=1e-6)
    assert snap["today_by_user"]["u1"]["cache_hit_rate"] == total["cache_hit_rate"]
    assert snap["sessions"]["s1"]["cache_hit_rate"] == total["cache_hit_rate"]


def test_usage_does_not_block_calls_and_unload_removes_ledger():
    ctx, store = usage_ctx()
    llm = ctx.require(LLM_SERVICE)
    ask(llm, "s1", "u1")
    assert store.snapshot()["totals"]["calls"] == 1

    ctx.unload_plugin("llm-tokens-usages")
    assert ctx.get("llm-tokens-usages") is None
    ask(llm, "s1", "u1")  # 统计没了，调用照常
    assert ctx.get("llm-tokens-usages") is None


def test_usage_counts_calls_without_usage():
    """适配器没回 usage：仍计入调用次数（"有调用却零 token"要能被看见）。"""

    class NoUsageProvider:
        def models(self):
            from plugins.capability.llm.fake import fake_model

            return {info.model: info for info in [fake_model("m", provider="nou")]}

        def stream(self, request):
            yield TextDelta("没用量")
            yield Finish(reason="stop", usage=None)

    ctx, store = usage_ctx()
    ctx.require(LLM_SERVICE).register("nou", NoUsageProvider())
    request = ctx.require(LLM_SERVICE).request(
        messages(), provider="nou", model="m", session_id="s1", user_id="u1"
    )
    list(ctx.require(LLM_SERVICE).stream(request))
    snap = store.snapshot()
    assert snap["totals"]["calls"] == 1
    assert snap["totals"]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# 组装：三个子插件可以独立装卸
# ---------------------------------------------------------------------------


def test_sub_plugins_are_independent():
    ctx = boot_llm(retry=True, usage=True)
    ctx.load_plugin(fake_llm_plugin())
    llm = ctx.require(LLM_SERVICE)

    ask(llm, "s1", "u1")
    assert ctx.require("llm-tokens-usages").snapshot()["totals"]["calls"] == 1

    ctx.unload_plugin("llm-retry")       # 卸重试：不影响调用与统计
    ask(llm, "s1", "u1")
    assert ctx.require("llm-tokens-usages").snapshot()["totals"]["calls"] == 2

    ctx.unload_plugin("llm-tokens-usages")
    ask(llm, "s1", "u1")                  # 卸统计：调用照常
    assert llm.list_providers() == ("fake",)
