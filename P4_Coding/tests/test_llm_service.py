"""M2 第一步验收：LLM 路由层 + 假适配器。

钉住三件事（0044）：

    1. 一个 llm 服务按路由分发；适配器是独立插件，装卸不动别人；
    2. 模型选择是**请求数据**——不同会话 / 子代理用不同模型不需要额外机制；
    3. 模型能力（上下文窗口 / 输出上限 / 推理档位）按请求选中的模型解析。
"""

from __future__ import annotations

import pytest

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    Finish,
    InvalidRequestError,
    LLMRuntime,
    Message,
    UnknownModelError,
    UnknownProviderError,
    assemble_text,
    final_usage,
    llm_plugin,
)
from plugins.capability.llm.fake import (
    FakeProvider,
    fake_llm_plugin,
    fake_model,
)
from plugins.capability.llm.provider import llm_provider_plugin


def booted(**llm_config) -> tuple[Context, LLMRuntime]:
    """装好 llm 路由层；默认路由指向假模型（大多数用例都直接构造请求）。"""
    llm_config.setdefault("default_provider", "fake")
    llm_config.setdefault("default_model", "fake-1")
    ctx = Context()
    ctx.load_plugin(llm_plugin(**llm_config))
    return ctx, ctx.require(LLM_SERVICE)


def messages(text: str = "你好") -> tuple[Message, ...]:
    return (Message(role="user", content=text),)


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------


def test_register_list_and_unregister_route():
    _ctx, llm = booted()
    provider = FakeProvider()
    off = llm.register("fake", provider)
    assert llm.list_providers() == ("fake",)

    off()
    assert llm.list_providers() == ()
    off()  # 幂等
    assert llm.list_providers() == ()


def test_duplicate_route_fails_loud_and_keeps_original():
    _ctx, llm = booted()
    first = FakeProvider(replies=["一号"])
    second = FakeProvider(replies=["二号"])
    llm.register("fake", first)

    with pytest.raises(ValueError, match="已注册"):
        llm.register("fake", second)

    chunks = list(llm.stream(llm.request(messages(), provider="fake", model="fake-1")))
    assert assemble_text(chunks) == "一号"  # 原适配器原样还在


def test_unknown_provider_and_model_fail_loud_with_stable_codes():
    _ctx, llm = booted()
    llm.register("fake", FakeProvider())

    with pytest.raises(UnknownProviderError) as err:
        llm.stream(llm.request(messages(), provider="nope", model="x"))
    assert err.value.code == "NO_PROVIDER"

    with pytest.raises(UnknownModelError) as err:
        llm.stream(llm.request(messages(), provider="fake", model="nope"))
    assert err.value.code == "NO_MODEL"


def test_unregister_before_registration_identity_is_honored():
    """句柄认这一次注册：注销后重新注册同名路由，旧句柄不会误删新适配器。"""
    _ctx, llm = booted()
    old = llm.register("fake", FakeProvider(replies=["旧"]))
    old()

    llm.register("fake", FakeProvider(replies=["新"]))
    old()  # 旧句柄：不是现在这条注册 → 空操作

    chunks = list(llm.stream(llm.request(messages(), provider="fake", model="fake-1")))
    assert assemble_text(chunks) == "新"


# ---------------------------------------------------------------------------
# 请求构造与模型选择
# ---------------------------------------------------------------------------


def test_default_route_is_used_when_request_omits_it():
    _ctx, llm = booted(default_provider="fake", default_model="fake-1")
    provider = FakeProvider()
    llm.register("fake", provider)

    request = llm.request(messages())
    assert (request.provider, request.model) == ("fake", "fake-1")
    list(llm.stream(request))
    assert provider.calls[0].model == "fake-1"


def test_request_overrides_default_route():
    """会话 / 子代理的模型选择：只是在请求里写另一组 provider/model。"""
    _ctx, llm = booted(default_provider="fake", default_model="small")
    small = FakeProvider(models=[fake_model("small")], replies=["小模型"])
    large = FakeProvider(models=[fake_model("large")], replies=["大模型"])
    llm.register("fake", small)
    llm.register("big", large)

    session_a = llm.request(messages("甲"))
    session_b = llm.request(messages("乙"), provider="big", model="large")

    assert assemble_text(list(llm.stream(session_a))) == "小模型"
    assert assemble_text(list(llm.stream(session_b))) == "大模型"
    assert small.calls[-1].model == "small"
    assert large.calls[-1].model == "large"


def test_request_requires_route_when_no_default():
    _ctx, llm = booted(default_provider=None, default_model=None)
    with pytest.raises(InvalidRequestError, match="provider/model"):
        llm.request(messages())


# ---------------------------------------------------------------------------
# 模型能力解析 / 参数校验
# ---------------------------------------------------------------------------


def test_resolve_returns_the_selected_models_capabilities():
    _ctx, llm = booted()
    llm.register(
        "fake",
        FakeProvider(
            models=[
                fake_model("small", context_window=8_192, max_output_tokens=1_024),
                fake_model("huge", context_window=1_000_000, max_output_tokens=8_192),
            ]
        ),
    )
    assert llm.resolve("fake", "small").context_window == 8_192
    assert llm.resolve("fake", "huge").context_window == 1_000_000


def test_max_tokens_defaults_from_model_info_without_mutating_request():
    _ctx, llm = booted()
    llm.register("fake", FakeProvider(models=[fake_model(max_output_tokens=4_096)]))
    request = llm.request(messages())
    effective = llm.validate(request)
    assert effective.max_tokens == 4_096
    assert request.max_tokens is None  # 原请求没被改（frozen 数据的语义）


def test_explicit_max_tokens_wins():
    _ctx, llm = booted()
    provider = FakeProvider(models=[fake_model(max_output_tokens=4_096)])
    llm.register("fake", provider)
    list(llm.stream(llm.request(messages(), max_tokens=128)))
    assert provider.calls[0].max_tokens == 128


def test_reasoning_effort_validated_against_model():
    _ctx, llm = booted()
    llm.register(
        "fake",
        FakeProvider(models=[fake_model(reasoning_efforts=("low", "high"))]),
    )
    list(llm.stream(llm.request(messages(), reasoning_effort="high")))
    with pytest.raises(InvalidRequestError, match="推理档位"):
        list(llm.stream(llm.request(messages(), reasoning_effort="medium")))


def test_reasoning_effort_passes_for_models_that_do_not_declare_any():
    """模型没公布档位 = 这个参数不适用，不拦（由适配器/服务端决定）。"""
    _ctx, llm = booted()
    provider = FakeProvider(models=[fake_model(reasoning_efforts=())])
    llm.register("fake", provider)
    list(llm.stream(llm.request(messages(), reasoning_effort="whatever")))
    assert provider.calls[0].reasoning_effort == "whatever"


def test_temperature_range_is_checked():
    _ctx, llm = booted()
    llm.register("fake", FakeProvider())
    with pytest.raises(InvalidRequestError, match="temperature"):
        list(llm.stream(llm.request(messages(), temperature=3.0)))


# ---------------------------------------------------------------------------
# 流
# ---------------------------------------------------------------------------


def test_stream_yields_text_then_finish_with_usage():
    _ctx, llm = booted()
    llm.register("fake", FakeProvider(replies=["你好，世界"]))
    chunks = list(llm.stream(llm.request(messages())))
    assert assemble_text(chunks) == "你好，世界"
    assert isinstance(chunks[-1], Finish)
    assert chunks[-1].reason == "stop"
    assert final_usage(chunks) is not None


# ---------------------------------------------------------------------------
# 作为插件：装配 / 卸载 / 热替换
# ---------------------------------------------------------------------------


def test_adapters_are_plugins_and_loading_is_order_independent():
    ctx = Context()
    ctx.load_plugin(fake_llm_plugin(replies=["先装适配器"]))
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))

    llm = ctx.require(LLM_SERVICE)
    assert llm.list_providers() == ("fake",)
    chunks = list(llm.stream(llm.request(messages())))
    assert assemble_text(chunks) == "先装适配器"


def test_unloading_adapter_removes_its_route():
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    ctx.load_plugin(fake_llm_plugin())
    llm = ctx.require(LLM_SERVICE)
    assert llm.list_providers() == ("fake",)

    ctx.unload_plugin("llm-fake:fake")
    assert llm.list_providers() == ()
    with pytest.raises(UnknownProviderError):
        list(llm.stream(llm.request(messages())))


def test_replacing_adapter_keeps_the_llm_service_alive():
    """热替换适配器（loader.update 的底层形状）：llm 服务不动，路由换人。"""
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    ctx.load_plugin(fake_llm_plugin(replies=["旧适配器"]))
    llm = ctx.require(LLM_SERVICE)

    ctx.unload_plugin("llm-fake:fake")
    ctx.load_plugin(fake_llm_plugin(replies=["新适配器"]))

    assert ctx.require(LLM_SERVICE) is llm  # 路由层没有被重建
    assert assemble_text(list(llm.stream(llm.request(messages())))) == "新适配器"


def test_llm_plugin_can_be_disabled_and_reenabled():
    ctx = Context()
    ctx.load_plugin(llm_plugin())  # 不需要默认路由也能起
    ctx.disable_plugin("llm")
    assert ctx.get(LLM_SERVICE) is None
    ctx.enable_plugin("llm")
    assert ctx.require(LLM_SERVICE) is not None


# ---------------------------------------------------------------------------
# 装配单：llm 是 base，适配器是 capability
# ---------------------------------------------------------------------------


def test_assemble_with_roles():
    from plugins.base.loader import LOADER_SERVICE, loader_plugin

    ctx = Context()
    ctx.load_plugin(
        loader_plugin(
            {
                "llm": llm_plugin,
                "llm-provider": llm_provider_plugin,
            }
        )
    )
    loader = ctx.require(LOADER_SERVICE)
    states = loader.assemble(
        [
            {"id": "llm", "name": "llm", "role": "base",
             "config": {"default_provider": "fake", "default_model": "fake-1"}},
            {"id": "provider", "name": "llm-provider", "role": "capability",
             "config": {
                 "route": "fake",
                 "base_url": "http://example.invalid/chat",
                 "api_key": "k",
                 "models": [{"id": "fake-1", "context_window": 8192}],
                 "transport": lambda *a, **k: [
                     'data: {"choices":[{"delta":{"content":"装配单来的"}}]}',
                     "data: [DONE]",
                 ],
             }},
        ]
    )
    assert [s.status for s in states] == ["active", "active"]
    assert [s.role for s in states] == ["base", "capability"]

    chunks = list(ctx.require(LLM_SERVICE).stream(ctx.require(LLM_SERVICE).request(messages())))
    assert assemble_text(chunks) == "装配单来的"


def test_assemble_fails_loud_when_llm_has_no_provider():
    """`llm` 声明了插件级依赖 llm-provider：只有假适配器不算完整（0045 补充）。"""
    from plugins.base.loader import LOADER_SERVICE, AssemblyError, loader_plugin

    ctx = Context()
    ctx.load_plugin(loader_plugin({"llm": llm_plugin, "llm-fake": fake_llm_plugin}))
    loader = ctx.require(LOADER_SERVICE)

    with pytest.raises(AssemblyError, match="插件级依赖.*llm-provider"):
        loader.assemble(
            [
                {"id": "llm", "name": "llm", "role": "base"},
                {"id": "fake", "name": "llm-fake", "role": "capability"},
            ]
        )
    # 整批回滚：连 llm 也不留下
    assert loader.snapshot() == ()
    assert ctx.services == [LOADER_SERVICE]


def test_loaded_llm_without_provider_is_still_reported_at_runtime():
    """绕过 loader 直接 load_plugin 时不做装配校验；台账里能看到缺什么。"""
    ctx = Context()
    ctx.load_plugin(llm_plugin())
    info = next(item for item in ctx.plugins if item.name == "llm")
    assert info.requires == ("llm-provider",)
    assert info.missing_requires == ("llm-provider",)
