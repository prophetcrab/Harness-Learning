"""图片能力验收：内容块 + 模型能力校验 + 方言编码。

钉住四件事：

    1. 消息可以带图片（url 或 bytes 两种来源），纯文本消息保持原样；
    2. **模型不收图时在发请求之前就拒掉**（不是等服务端报错）；
    3. OpenAI 兼容方言把内容块翻成多模态数组；bytes 编码成 data URL；
    4. 统计把图片折算进输入 token（不漏计）。
"""

from __future__ import annotations

import base64
from collections.abc import Iterable

import pytest

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    ImagePart,
    InvalidRequestError,
    Message,
    ModelInfo,
    TextPart,
    assemble_text,
    build_payload,
    content_payload,
    final_usage,
    llm_plugin,
)
from plugins.capability.llm.fake import (
    IMAGE_TOKEN_ESTIMATE,
    FakeProvider,
    fake_model,
)
from plugins.capability.llm.provider import llm_provider_plugin


def messages(*content: str) -> tuple[Message, ...]:
    return tuple(Message(role="user", content=item) for item in content)


def booted(*, images: bool) -> tuple[Context, object]:
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    info = fake_model("fake-1", context_window=8_192)
    if images:
        info = ModelInfo(
            provider=info.provider,
            model=info.model,
            context_window=info.context_window,
            max_output_tokens=info.max_output_tokens,
            supports_images=True,
        )
    provider = FakeProvider(models=[info], replies=["收到"])
    ctx.require(LLM_SERVICE).register("fake", provider)
    return ctx, ctx.require(LLM_SERVICE)


# ---------------------------------------------------------------------------
# 消息形态
# ---------------------------------------------------------------------------


def test_plain_text_message_still_works():
    message = Message(role="user", content="你好")
    assert message.text == "你好"
    assert message.images == ()
    assert message.has_images is False


def test_message_with_text_and_image_parts():
    image = ImagePart(url="https://example.com/cat.png")
    message = Message(role="user", content=(TextPart("这是什么？"), image))
    assert message.text == "这是什么？"
    assert message.images == (image,)
    assert message.has_images is True


def test_image_requires_exactly_one_source():
    with pytest.raises(ValueError):
        ImagePart()  # 两个都没有
    with pytest.raises(ValueError):
        ImagePart(url="https://x", data=b"bytes")  # 两个都有
    with pytest.raises(TypeError):
        ImagePart(url=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ImagePart(data="不是 bytes")  # type: ignore[arg-type]


def test_message_rejects_unknown_parts():
    with pytest.raises(TypeError):
        Message(role="user", content=("不是内容块",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Message(role="user", content=())


# ---------------------------------------------------------------------------
# 模型能力校验
# ---------------------------------------------------------------------------


def test_image_request_accepted_by_capable_model():
    _ctx, llm = booted(images=True)
    request = llm.request(
        (Message(role="user", content=(TextPart("看图"), ImagePart(url="https://x/a.png"))),)
    )
    chunks = list(llm.stream(request))
    assert assemble_text(chunks) == "收到"


def test_image_rejected_before_calling_a_text_only_model():
    ctx, llm = booted(images=False)
    request = llm.request(
        (Message(role="user", content=(TextPart("看图"), ImagePart(url="https://x/a.png"))),)
    )
    with pytest.raises(InvalidRequestError, match="不接受图片"):
        list(llm.stream(request))
    # 请求没有发出去：假 provider 一次都没被调用
    provider = llm._providers["fake"]
    assert provider.calls == []


def test_text_only_request_still_works_on_text_only_model():
    _ctx, llm = booted(images=False)
    assert assemble_text(list(llm.stream(llm.request(messages("纯文本"))))) == "收到"


# ---------------------------------------------------------------------------
# 方言编码
# ---------------------------------------------------------------------------


def test_content_payload_for_text_is_a_plain_string():
    assert content_payload("你好") == "你好"


def test_content_payload_translates_parts():
    payload = content_payload(
        (
            TextPart("看这张"),
            ImagePart(url="https://x/a.png", detail="high"),
        )
    )
    assert payload == [
        {"type": "text", "text": "看这张"},
        {
            "type": "image_url",
            "image_url": {"url": "https://x/a.png", "detail": "high"},
        },
    ]


def test_content_payload_encodes_bytes_as_data_url():
    raw = b"\x89PNG\r\n\x1a\nfake"
    payload = content_payload((ImagePart(data=raw, media_type="image/png"),))
    url = payload[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == raw


def test_build_payload_uses_multimodal_content():
    from plugins.base.llm import LLMRequest

    request = LLMRequest(
        provider="p",
        model="m",
        messages=(
            Message(role="user", content="纯文本"),
            Message(role="user", content=(TextPart("图"), ImagePart(url="https://x/b.png"))),
        ),
    )
    payload = build_payload(request)
    assert payload["messages"][0]["content"] == "纯文本"
    assert isinstance(payload["messages"][1]["content"], list)
    assert payload["messages"][1]["content"][1]["type"] == "image_url"


def test_provider_sends_multimodal_payload():
    captured: dict = {}

    def transport(url, headers, payload, timeout) -> Iterable[str]:
        captured.update(payload)
        return ['data: {"choices":[{"delta":{"content":"看到了"}}]}', "data: [DONE]"]

    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="real", default_model="vision-1"))
    ctx.load_plugin(
        llm_provider_plugin(
            route="real",
            base_url="https://api.example/chat",
            api_key="k",
            models=[{"id": "vision-1", "context_window": 128000, "supports_images": True}],
            transport=transport,
        )
    )
    llm = ctx.require(LLM_SERVICE)

    request = llm.request(
        (Message(role="user", content=(TextPart("这是什么"), ImagePart(url="https://x/c.png"))),)
    )
    assert assemble_text(list(llm.stream(request))) == "看到了"
    content = captured["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1]["image_url"]["url"] == "https://x/c.png"


# ---------------------------------------------------------------------------
# 用量估算
# ---------------------------------------------------------------------------


def test_image_counts_toward_input_tokens():
    info = fake_model("fake-1")
    vision = ModelInfo(
        provider=info.provider,
        model=info.model,
        context_window=info.context_window,
        max_output_tokens=info.max_output_tokens,
        supports_images=True,
    )
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    ctx.require(LLM_SERVICE).register("fake", FakeProvider(models=[vision], replies=["好"]))

    text_only = _request(ctx, "四个字")
    with_image = _request(ctx, (TextPart("四个字"), ImagePart(url="https://x/d.png")))

    chunks_text = list(ctx.require(LLM_SERVICE).stream(text_only))
    chunks_image = list(ctx.require(LLM_SERVICE).stream(with_image))

    assert final_usage(chunks_image).input_tokens == (
        final_usage(chunks_text).input_tokens + IMAGE_TOKEN_ESTIMATE
    )


def _request(ctx, content):
    return ctx.require(LLM_SERVICE).request((Message(role="user", content=content),))
