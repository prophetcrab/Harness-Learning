"""llm-fake —— 假模型适配器（能力插件）：离线测试与离线产品的模型接入。

它注册一条路由（默认 `fake`），模型信息与剧本都由 config 决定：

    {"id": "llm-fake", "name": "llm-fake",
     "config": {"route": "fake", "model": "fake-1", "context_window": 8192,
                "replies": ["你好"]}}

真实适配器（`llm-provider`）在同一个形状上：依赖 `llm` 服务、在 setup 里
`register`，返回它的注销句柄。

类型复用 `plugins.base.llm` 的公开定义（能力插件 → 基座插件是单向依赖，
不形成环；决策 0052）。这样 `isinstance(chunk, Finish)` 在测试与消费方
眼里是同一个类。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    Chunk,
    Finish,
    LLMRequest,
    Message,
    ModelInfo,
    TextDelta,
    ToolCall,
    ToolCallsDelta,
    UnknownModelError,
    Usage,
)

#: 一张图粗略折算的输入 token（真实值取决于分辨率与档位；测试只要稳定）。
IMAGE_TOKEN_ESTIMATE = 85


def _input_token_estimate(messages: Iterable[Message]) -> int:
    """粗估输入 token：文本按字符数 / 4，图片按固定值折算。"""
    total = 0
    for message in messages:
        total += len(message.text) // 4
        total += IMAGE_TOKEN_ESTIMATE * len(message.images)
    return total


@dataclass(frozen=True)
class ScriptedReply:
    """一条预设回复。

    `text` 原样吐给调用方；`tool_calls` 非空表示"这一轮模型发起工具调用"
    （此时 `text` 可以是空串）。用量缺省按字符数粗估。
    """

    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        if not self.text and not self.tool_calls:
            raise ValueError("ScriptedReply 至少要给 text 或 tool_calls 之一")


class FakeProvider:
    """剧本 provider：按顺序吐出回复，并记下每次请求（`calls`）。"""

    def __init__(
        self,
        models: Iterable[ModelInfo] | None = None,
        replies: Iterable[ScriptedReply | str] | None = None,
        *,
        on_exhausted: str = "cycle",
        cached_input_tokens: int | None = None,
    ) -> None:
        self._models = {info.model: info for info in (models or [fake_model()])}
        self._replies = [
            reply if isinstance(reply, ScriptedReply) else ScriptedReply(text=reply)
            for reply in (replies or ["（假模型的回复）"])
        ]
        if on_exhausted not in ("cycle", "error"):
            raise ValueError(
                f"on_exhausted 只能是 'cycle' / 'error'，收到：{on_exhausted!r}"
            )
        self._on_exhausted = on_exhausted
        #: 模拟缓存命中（用量统计的测试用）：None = 不命中。
        self._cached_input_tokens = cached_input_tokens
        self._index = 0
        #: 每次调用收到的请求（按调用顺序）。
        self.calls: list[LLMRequest] = []

    def models(self) -> Mapping[str, ModelInfo]:
        return dict(self._models)

    def stream(self, request: LLMRequest) -> Iterator[Chunk]:
        """按剧本吐一条回复（文本增量 + Finish）。"""
        if request.model not in self._models:
            raise UnknownModelError(f"假 provider 没有模型：{request.model!r}")
        self.calls.append(request)
        if self._index >= len(self._replies):
            if self._on_exhausted == "error":
                raise RuntimeError(
                    f"假 provider 的剧本用完了（已调用 {len(self.calls)} 次）"
                )
            self._index = 0
        reply = self._replies[self._index]
        self._index += 1
        if reply.text:
            yield TextDelta(reply.text)
        if reply.tool_calls:
            # 模拟流式分片：每个调用拆成"声明"与"参数增量"两块，验证聚合逻辑。
            for index, call in enumerate(reply.tool_calls):
                half = len(call.arguments) // 2
                yield ToolCallsDelta(
                    deltas=(
                        {
                            "index": index,
                            "id": call.id,
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments[:half],
                            },
                        },
                    )
                )
                if len(call.arguments) > half:
                    yield ToolCallsDelta(
                        deltas=(
                            {
                                "index": index,
                                "function": {"arguments": call.arguments[half:]},
                            },
                        )
                    )
        input_tokens = (
            reply.input_tokens
            if reply.input_tokens is not None
            else _input_token_estimate(request.messages)
        )
        yield Finish(
            reason="tool_calls" if reply.tool_calls else "stop",
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=reply.output_tokens
                if reply.output_tokens is not None
                else len(reply.text) // 4,
                cached_input_tokens=(
                    self._cached_input_tokens
                    if self._cached_input_tokens is not None
                    else 0
                ),
            ),
        )


def fake_model(
    model: str = "fake-1",
    *,
    provider: str = "fake",
    context_window: int = 8_192,
    max_output_tokens: int | None = 1_024,
    reasoning_efforts: tuple[str, ...] = (),
) -> ModelInfo:
    """造一个假模型的元信息（默认值足够跑通一个 turn 的测试）。"""
    return ModelInfo(
        provider=provider,
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        reasoning_efforts=reasoning_efforts,
    )


class FakeLLMPlugin:
    """注册一条假模型路由。"""

    name = "llm-fake"
    inject = (LLM_SERVICE,)

    def __init__(
        self,
        *,
        route: str = "fake",
        model: str = "fake-1",
        context_window: int = 8_192,
        max_output_tokens: int | None = 1_024,
        reasoning_efforts: Sequence[str] = (),
        replies: Sequence[str] = ("（假模型的回复）",),
        on_exhausted: str = "cycle",
        cached_input_tokens: int | None = None,
    ) -> None:
        # 插件名带上路由：一个进程里可以同时挂多条假路由。
        self.name = f"llm-fake:{route}"
        self._route = route
        self._info = fake_model(
            model,
            provider=route,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            reasoning_efforts=tuple(reasoning_efforts),
        )
        self._replies = list(replies)
        self._on_exhausted = on_exhausted
        self._cached_input_tokens = cached_input_tokens

    def setup(self, ctx: Context):
        runtime = ctx.require(LLM_SERVICE)
        provider = FakeProvider(
            models=[self._info],
            replies=self._replies,
            on_exhausted=self._on_exhausted,
            cached_input_tokens=self._cached_input_tokens,
        )
        # 返回注销句柄：适配器卸载时路由随之消失（注册即回卷）。
        return runtime.register(self._route, provider)


def fake_llm_plugin(**config: object) -> FakeLLMPlugin:
    """工厂：装配单的 `config` 直接喂给构造参数。"""
    return FakeLLMPlugin(**config)  # type: ignore[arg-type]


__all__ = [
    "FakeLLMPlugin",
    "FakeProvider",
    "ScriptedReply",
    "fake_llm_plugin",
    "fake_model",
]
