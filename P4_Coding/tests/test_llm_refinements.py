"""LLM 组的三处完善验收：流式安全的重试 / 日合计 / 统计只读。

    1. 重试保持流式：块一到就交出去（不物化），首块之后出错不重试（避免重复输出）；
    2. 日维度有**合计**（today_total），和按用户的桶并列；
    3. `llm-tokens-usages` 服务是只读视图：只有 snapshot / 便利属性，没有 record。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    Finish,
    LLMConnectionError,
    Message,
    TextDelta,
    assemble_text,
    llm_plugin,
)
from plugins.capability.llm.retry import llm_retry_plugin
from plugins.capability.llm.tokens_usages import (
    TokenUsageStore,
    TokenUsageView,
    llm_tokens_usages_plugin,
)


def messages(text: str = "你好") -> tuple[Message, ...]:
    return (Message(role="user", content=text),)


# ---------------------------------------------------------------------------
# 1. 流式安全的重试
# ---------------------------------------------------------------------------


class StreamingProvider:
    """可编排的假适配器：第 N 次尝试按 `script` 吐块 / 抛错。"""

    def __init__(self, script: list[list[Any]]) -> None:
        self._script = script
        self.attempts = 0

    def models(self):
        from plugins.capability.llm.fake import fake_model

        return {info.model: info for info in [fake_model("m", provider="s")]}

    def stream(self, request) -> Iterator:
        index = min(self.attempts, len(self._script) - 1)
        self.attempts += 1
        for item in self._script[index]:
            if isinstance(item, BaseException):
                raise item
            yield item


def boot_with(script: list[list[Any]], **retry_config) -> tuple[Context, StreamingProvider]:
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="s", default_model="m"))
    ctx.load_plugin(llm_retry_plugin(**retry_config))
    provider = StreamingProvider(script)
    ctx.require(LLM_SERVICE).register("s", provider)
    return ctx, provider


def test_retry_streams_the_first_chunk_immediately():
    """重试插件不能把流物化：拿到第一块就要能立刻交出去。"""
    ctx, _provider = boot_with(
        [[TextDelta("第一块"), TextDelta("第二块"), Finish(reason="stop")]],
        max_attempts=2,
    )
    stream = ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages()))
    first = next(stream)  # 不 list() 也能拿到第一块
    assert isinstance(first, TextDelta) and first.text == "第一块"
    assert assemble_text([first, *stream]) == "第一块第二块"


def test_retry_does_not_duplicate_output_when_failing_after_first_chunk():
    """首块之后出错：不重试（否则已输出的内容会被再吐一遍）。"""
    ctx, provider = boot_with(
        [
            [TextDelta("已经说了一半"), LLMConnectionError("断线")],
            [TextDelta("重试的完整回答"), Finish(reason="stop")],
        ],
        max_attempts=3,
    )
    stream = ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages()))
    assert next(stream).text == "已经说了一半"
    with pytest.raises(LLMConnectionError):
        list(stream)
    assert provider.attempts == 1  # 没有发起第二次


def test_retry_still_recovers_when_failure_is_before_any_chunk():
    ctx, provider = boot_with(
        [
            [LLMConnectionError("断线")],
            [TextDelta("第二次成功"), Finish(reason="stop")],
        ],
        max_attempts=2,
    )
    chunks = list(ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages())))
    assert assemble_text(chunks) == "第二次成功"
    assert provider.attempts == 2


def test_retry_retries_error_finish_before_output():
    ctx, provider = boot_with(
        [
            [Finish(reason="error", error="上游炸了", error_code="LLM_SERVER_ERROR")],
            [TextDelta("好的"), Finish(reason="stop")],
        ],
        max_attempts=2,
    )
    chunks = list(ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages())))
    assert assemble_text(chunks) == "好的"
    assert provider.attempts == 2


def test_exhausted_retries_fail_loudly():
    ctx, provider = boot_with(
        [[LLMConnectionError("一直断")]],
        max_attempts=3,
    )
    # 用尽重试：抛出最后一次的原始异常（保留 code，消费方按它分支）
    with pytest.raises(LLMConnectionError, match="一直断"):
        list(ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages())))
    assert provider.attempts == 3


# ---------------------------------------------------------------------------
# 2. 日合计
# ---------------------------------------------------------------------------


def make_usage_ctx() -> tuple[Context, Any]:
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    ctx.load_plugin(llm_tokens_usages_plugin())
    ctx.load_plugin(
        __import__(
            "plugins.capability.llm.fake", fromlist=["fake_llm_plugin"]
        ).fake_llm_plugin(replies=["好"], cached_input_tokens=10)
    )
    return ctx, ctx.require("llm-tokens-usages")


def ask(ctx: Context, session: str | None, user: str | None) -> None:
    llm = ctx.require(LLM_SERVICE)
    list(llm.stream(llm.request(messages(), session_id=session, user_id=user)))


def test_today_total_exists_and_equals_sum_of_users():
    ctx, view = make_usage_ctx()
    ask(ctx, "s1", "alice")
    ask(ctx, "s1", "bob")

    snap = view.snapshot()
    assert "today_total" in snap
    today = snap["today_total"]
    assert today["calls"] == 2
    assert today["total_tokens"] == (
        snap["today_by_user"]["alice"]["total_tokens"]
        + snap["today_by_user"]["bob"]["total_tokens"]
    )
    assert today["cached_input_tokens"] == (
        snap["today_by_user"]["alice"]["cached_input_tokens"]
        + snap["today_by_user"]["bob"]["cached_input_tokens"]
    )


def test_today_total_rolls_over_at_day_boundary():
    store = TokenUsageStore()
    view = TokenUsageView(store)

    class Request:
        session_id = "s"
        user_id = "u"

    from plugins.base.llm import Usage

    store.record(Request(), Usage(input_tokens=10, output_tokens=5, cached_input_tokens=2))
    assert view.today["total_tokens"] == 15

    # 模拟跨天：把账本的日期改成昨天，再记一笔 → 今日桶清零
    store.today = date.today() - timedelta(days=1)
    store.record(Request(), Usage(input_tokens=1, output_tokens=1))
    assert view.today["total_tokens"] == 2
    assert view.totals["total_tokens"] == 17  # 总量保留


# ---------------------------------------------------------------------------
# 3. 统计只读
# ---------------------------------------------------------------------------


def test_service_exposes_a_read_only_view():
    _ctx, view = make_usage_ctx()
    assert isinstance(view, TokenUsageView)
    assert not hasattr(view, "record")
    assert not hasattr(view, "users_today")
    assert repr(view) == "TokenUsageView(<只读>)"


def test_view_has_convenience_properties():
    ctx, view = make_usage_ctx()
    ask(ctx, "s1", "u1")
    assert view.totals["calls"] == 1
    assert view.today["calls"] == 1
    assert view.sessions["s1"]["calls"] == 1
    assert view.snapshot()["today"] == date.today().isoformat()


def test_view_snapshot_is_a_copy():
    """拿到的 dict 改不动账本（深拷贝到标量层）。"""
    ctx, view = make_usage_ctx()
    ask(ctx, "s1", "u1")
    snapshot = view.snapshot()
    snapshot["totals"]["calls"] = 999
    assert view.totals["calls"] == 1
