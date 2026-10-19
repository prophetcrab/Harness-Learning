"""llm —— 模型接入的**能力入口**：路由 + 请求校验 + 子插件报告。

按决策 0044 / 0045。它是"LLM 能力组"的主插件，与三个子插件配合：

    llm（本插件，role=base）      路由表 + 请求校验 + 模型信息解析 + 子插件状态
    llm-provider:<route>          通用适配器：方言翻译 + 连接 + 可定制参数
    llm-retry                     重试策略（挂在与 llm/call 的 waterfall 上）
    llm-tokens-usages             消耗统计（听 llm/finish；按会话 / 按用户分桶）

**不要求子插件齐全**（用户的要求）：缺了哪个都能跑，只是少一项能力；
`sub_plugins()` / `missing_sub_plugins()` 如实报告缺什么。判断依据是**插件台账**
（loader 的记录），不是服务槽——这样同一个子插件挂多份（如多条 provider 路由）
也能正确识别。

**模型选择是数据**（0044），三层覆盖：装配默认 → 会话（请求里带 session_id
与选定的 provider/model）→ 请求（子代理/临时切换）。

**两个事件插座**：

    llm/call    waterfall：`call(req) -> Iterator[Chunk]` 可被包装（重试挂这里）
    llm/finish  emit：一次调用结束（成功或失败），带 request / usage / chunks

本插件**不做**：重试、用量统计、协议翻译——它们各是一个子插件（0045）。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

from core import Context, Disposer

#: 路由服务的名字（适配器插件与 agent-loop 都依赖它）。
LLM_SERVICE = "llm"

#: LLM 能力组的三个子插件（插件名基名；provider 会带 `:<route>` 后缀）。
SUB_PLUGINS: tuple[str, ...] = ("llm-provider", "llm-retry", "llm-tokens-usages")

#: 事件名（消费方按这些名字订阅）。
LLM_CALL_EVENT = "llm/call"
LLM_FINISH_EVENT = "llm/finish"
LLM_ERROR_EVENT = "llm/error"


class LLMRuntime:
    """路由表 + 调用管道：请求按 route 分发，并经过事件插座。"""

    def __init__(
        self,
        ctx: Context,
        *,
        default_provider: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._providers: dict[str, LLMProvider] = {}
        self._default_provider = default_provider
        self._default_model = default_model

    # ------------------------------------------------------------------
    # 路由表
    # ------------------------------------------------------------------

    def register(self, route: str, provider: LLMProvider) -> Disposer:
        """注册一条路由；返回注销句柄（**认这一次注册**，幂等）。

        路由名重复 → ValueError（不静默覆盖：两个适配器抢同一个名字是配置错误）。
        注销后再注册同名路由是合法的（热替换适配器）。
        """
        if not isinstance(route, str) or not route:
            raise ValueError(f"路由名必须是非空字符串，收到：{route!r}")
        if route in self._providers:
            raise ValueError(
                f"路由已注册：{route!r}（重复注册直接报错；换适配器先注销旧的）"
            )
        if not callable(getattr(provider, "stream", None)):
            raise TypeError(f"provider 必须有 stream(request) 方法：{provider!r}")
        if not callable(getattr(provider, "models", None)):
            raise TypeError(f"provider 必须有 models() 方法：{provider!r}")
        self._providers[route] = provider

        def unregister() -> None:
            if self._providers.get(route) is provider:
                self._providers.pop(route, None)

        return unregister

    def list_providers(self) -> tuple[str, ...]:
        """已注册的路由名（按注册顺序）。"""
        return tuple(self._providers)

    def models(self, provider: str) -> tuple[ModelInfo, ...]:
        """某个路由支持的模型（按适配器给出的顺序）。"""
        adapter = self._adapter(provider)
        return tuple(adapter.models().values())

    # ------------------------------------------------------------------
    # 子插件状态（缺了哪些 —— 用户要求的报告）
    # ------------------------------------------------------------------

    def sub_plugins(self) -> dict[str, bool]:
        """三个子插件各自在不在（按插件台账判断，支持 `llm-provider:<route>`）。"""
        names = [info.name for info in self._ctx.plugins]
        return {
            base: any(name == base or name.startswith(f"{base}:") for name in names)
            for base in SUB_PLUGINS
        }

    def missing_sub_plugins(self) -> tuple[str, ...]:
        """缺了哪些子插件（空元组 = 齐全）。

        注意：`llm-provider` 同时是**插件级依赖**（`requires`）——loader 在装配
        期就会因它缺失而 fail loud。这里保留它是为了运行时（绕过 loader 直接
        load_plugin 的测试 / 嵌入式场景）也能看见"不完整"。
        """
        return tuple(name for name, present in self.sub_plugins().items() if not present)

    def status(self) -> dict[str, Any]:
        """给管理面 / dump 的一眼状态（含插件级依赖是否满足）。"""
        missing = self.missing_sub_plugins()
        return {
            "default_route": self.default_route,
            "routes": list(self.list_providers()),
            "sub_plugins": self.sub_plugins(),
            "missing_sub_plugins": list(missing),
            "requires_satisfied": "llm-provider" not in missing,
        }

    # ------------------------------------------------------------------
    # 请求构造 / 解析
    # ------------------------------------------------------------------

    @property
    def default_route(self) -> tuple[str, str] | None:
        """装配默认路由（可能还没注册——解析发生在请求时，顺序无关）。"""
        if self._default_provider is None or self._default_model is None:
            return None
        return (self._default_provider, self._default_model)

    def request(
        self,
        messages: Sequence[Message],
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_choice: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMRequest:
        """按"显式 > 装配默认"构造请求（会话层传自己的 provider/model 即可）。"""
        route_provider = provider or self._default_provider
        route_model = model or self._default_model
        if not route_provider or not route_model:
            raise InvalidRequestError(
                "请求没有指定 provider/model，装配也没有默认值（检查装配单的 llm 配置）"
            )
        # 边界归一：消息与工具可以是本模块的类型、字典，或任何"长得像"的对象
        # （其它插件用自己的副本构造）。这里统一转成本模块的内部类型。
        return LLMRequest(
            provider=route_provider,
            model=route_model,
            messages=tuple(_as_message(item) for item in messages),
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            tools=tuple(_as_tool_spec(item) for item in (tools or ())),
            tool_choice=tool_choice,
            session_id=session_id,
            user_id=user_id,
            extra=dict(extra or {}),
        )

    def resolve(self, provider: str, model: str) -> ModelInfo:
        """解析模型信息（未知路由 / 未知模型都 fail loud，带稳定 code）。"""
        adapter = self._adapter(provider)
        models = adapter.models()
        info = models.get(model)
        if info is None:
            known = "、".join(models) or "（适配器没有公布模型）"
            raise UnknownModelError(
                f"路由 {provider!r} 没有模型 {model!r}（可用：{known}）"
            )
        return info

    def validate(self, request: LLMRequest) -> LLMRequest:
        """校验请求并补齐默认调用参数，返回**生效的**请求（不改原对象）。"""
        info = self.resolve(request.provider, request.model)
        # 图片是模型能力：不收图的模型在**发请求之前**就拒掉（别等服务端报错，
        # 那样既浪费一次调用，也让"模型不支持"变成不透明的协议错误）。
        if not info.supports_images:
            image_messages = [
                index for index, message in enumerate(request.messages) if message.has_images
            ]
            if image_messages:
                raise InvalidRequestError(
                    f"模型 {info.model!r} 不接受图片输入，但消息 {image_messages} 带了图片"
                    "（换一个 supports_images=True 的模型，或去掉图片）"
                )
        max_tokens = request.max_tokens
        if max_tokens is None:
            max_tokens = info.max_output_tokens
        elif max_tokens <= 0:
            raise InvalidRequestError(f"max_tokens 必须为正数，收到：{max_tokens!r}")

        effort = request.reasoning_effort
        if effort is not None and info.reasoning_efforts and effort not in info.reasoning_efforts:
            raise InvalidRequestError(
                f"模型 {info.model!r} 不支持推理档位 {effort!r}"
                f"（可用：{'、'.join(info.reasoning_efforts)}）"

            )
        temperature = request.temperature
        if temperature is not None and not (0 <= temperature <= 2):
            raise InvalidRequestError(f"temperature 必须在 0–2 之间，收到：{temperature!r}")

        # 工具是模型能力：不支持 function calling 的模型带工具也要提前拒掉。
        if request.tools and not info.supports_tools:
            raise InvalidRequestError(
                f"模型 {info.model!r} 不支持工具调用，但请求带了 {len(request.tools)} 件工具"
            )
        if request.tool_choice is not None and not request.tools:
            raise InvalidRequestError("给了 tool_choice 但没有 tools")
        if request.tool_choice not in (None, "auto", "none", "required"):
            raise InvalidRequestError(
                f"tool_choice 只能是 auto / none / required，收到：{request.tool_choice!r}"

            )
        return LLMRequest(
            provider=request.provider,
            model=request.model,
            messages=request.messages,
            max_tokens=max_tokens,
            reasoning_effort=effort,
            temperature=temperature,
            tools=request.tools,
            tool_choice=request.tool_choice,
            session_id=request.session_id,
            user_id=request.user_id,
            extra=request.extra,
        )

    def stream(self, request: LLMRequest) -> Iterator[Chunk]:
        """按请求里的路由分发；参数先校验、默认值先补齐。

        管道（顺序固定）：

            validate → llm/call（waterfall，策略可包住调用）→ 消费流
                     → llm/finish（emit，计量/统计挂这里）

        流是**惰性**的：调用方不消费，就什么都不会发生。**逐块透传**——不在
        这里 collect（否则"流式"就名存实亡，消费方要等整段生成完才拿到第一块）。
        `llm/finish` 在流结束时发出，带最后一个 `Finish` 里的用量。
        """
        effective = self.validate(request)

        def call(req: LLMRequest) -> Iterator[Chunk]:
            return self._adapter(req.provider).stream(req)

        wrapped = self._ctx.waterfall(LLM_CALL_EVENT, call, request=effective)

        def run() -> Iterator[Chunk]:
            usage = None
            try:
                for chunk in wrapped(effective):
                    if getattr(chunk, "type", None) == "finish":
                        usage = chunk.usage
                    yield chunk
            except BaseException as exc:
                self._ctx.emit(LLM_ERROR_EVENT, request=effective, error=exc)
                raise
            self._ctx.emit(
                LLM_FINISH_EVENT,
                request=effective,
                usage=usage,
            )
        return run()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _adapter(self, provider: str) -> LLMProvider:
        adapter = self._providers.get(provider)
        if adapter is None:
            known = "、".join(self._providers) or "（还没有适配器）"
            raise UnknownProviderError(
                f"没有名为 {provider!r} 的模型适配器（已注册：{known}）"
            )
        return adapter


class LLMPlugin:
    """提供 `llm` 服务（能力入口）。

    装配单里应声明 `role="base"`。构造参数是**新会话的默认路由**，不是"唯一
    模型"——会话与请求都可以覆盖（0044）。

    三个子插件**不是服务依赖**（不在 inject 里）：LLM 主干在没有 provider /
    retry / 统计时照常提供路由与校验能力。但 `llm-provider` 是**插件级依赖**
    （`requires`）：没有适配器时任何调用都会失败，属于装配不完整——由 loader
    在装配 / 热插拔后 fail loud，而不是等到第一次请求（0045）。
    """

    name = "llm"
    #: 插件级依赖：至少要有一个 provider 实例（`llm-provider:<route>` 都算）。
    #: 不用 inject 的原因：provider 反过来依赖 llm 服务，两边 inject 会互相等死。
    requires = ("llm-provider",)

    def __init__(
        self,
        *,
        default_provider: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._default_provider = default_provider
        self._default_model = default_model

    def setup(self, ctx: Context) -> None:
        ctx.provide(
            LLM_SERVICE,
            LLMRuntime(
                ctx,
                default_provider=self._default_provider,
                default_model=self._default_model,
            ),
        )


def llm_plugin(
    *, default_provider: str | None = None, default_model: str | None = None
) -> LLMPlugin:
    """工厂：装配单的 `config` 直接喂给这里。"""
    return LLMPlugin(default_provider=default_provider, default_model=default_model)


# ---------------------------------------------------------------------------
# 边界归一（其它插件用自己的副本构造数据时用）
# ---------------------------------------------------------------------------


def _as_message(item: Any) -> Message:
    if isinstance(item, Message):
        return item
    if isinstance(item, Mapping):
        return Message(
            role=item["role"],
            content=item.get("content", ""),
            tool_calls=tuple(_as_tool_call(call) for call in item.get("tool_calls") or ()),
            tool_call_id=item.get("tool_call_id"),
        )
    return Message(
        role=item.role,
        content=item.content,
        tool_calls=tuple(_as_tool_call(call) for call in getattr(item, "tool_calls", ()) or ()),
        tool_call_id=getattr(item, "tool_call_id", None),
    )


def _as_tool_call(item: Any) -> ToolCall:
    if isinstance(item, ToolCall):
        return item
    if isinstance(item, Mapping):
        return ToolCall(
            id=str(item["id"]),
            name=str(item["name"]),
            arguments=str(item.get("arguments", "{}")),
        )
    return ToolCall(
        id=str(item.id),
        name=str(item.name),
        arguments=str(getattr(item, "arguments", "{}")),
    )


def _as_tool_spec(item: Any) -> ToolSpec:
    if isinstance(item, ToolSpec):
        return item
    if isinstance(item, Mapping):
        return ToolSpec(
            name=str(item["name"]),
            description=str(item.get("description", "")),
            parameters=dict(item.get("parameters") or {}),
        )
    return ToolSpec(
        name=str(item.name),
        description=str(getattr(item, "description", "")),
        parameters=dict(getattr(item, "parameters", {}) or {}),
    )


__all__ = [
    "Chunk",
    "Content",
    "ContentPart",
    "DONE",
    "Finish",
    "FinishReason",
    "ImagePart",
    "InvalidRequestError",
    "LLMAuthError",
    "LLMConnectionError",
    "LLMError",
    "LLMPlugin",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMRequest",
    "LLMRuntime",
    "LLMServerError",
    "LLMTimeoutError",
    "LLM_CALL_EVENT",
    "LLM_ERROR_EVENT",
    "LLM_FINISH_EVENT",
    "LLM_SERVICE",
    "Message",
    "ModelInfo",
    "Role",
    "SUB_PLUGINS",
    "TextDelta",
    "TextPart",
    "ToolCall",
    "ToolCallAccumulator",
    "ToolCallsDelta",
    "ToolSpec",
    "UnknownModelError",
    "UnknownProviderError",
    "Usage",
    "assemble_text",
    "build_payload",
    "content_payload",
    "final_usage",
    "finish_reason",
    "is_done",
    "llm_plugin",
    "message_payload",
    "parse_data_line",
    "parse_stream",
    "text_delta",
    "tool_call_deltas",
    "tool_payload",
    "usage_from_event",
]


# ======================================================================
# 以下合并自 components/llm/provider.py
# ======================================================================


#: 消息角色（与各家 API 的常用词汇对齐）。
Role = Literal["system", "user", "assistant", "tool"]

#: 一次调用的结束原因。
FinishReason = Literal["stop", "length", "tool_calls", "error", "aborted"]


class LLMError(RuntimeError):
    """LLM 接缝的错误基类。`code` 是稳定标识（消费方按它分支，不解析文案）。"""

    code = "LLM_ERROR"


class UnknownProviderError(LLMError):
    """请求点名了一个没有适配器的路由。"""

    code = "NO_PROVIDER"


class UnknownModelError(LLMError):
    """适配器不认识请求里的模型。"""

    code = "NO_MODEL"


class InvalidRequestError(LLMError):
    """请求参数不合法（窗口越界、推理档位不支持等）。"""

    code = "INVALID_REQUEST"


class LLMConnectionError(LLMError):
    """连不上 / 连接中断（可重试）。"""

    code = "LLM_CONNECTION"


class LLMTimeoutError(LLMError):
    """调用超时（可重试）。"""

    code = "LLM_TIMEOUT"


class LLMRateLimitError(LLMError):
    """被限流（可重试，通常要退避）。"""

    code = "LLM_RATE_LIMIT"


class LLMServerError(LLMError):
    """上游 5xx（可重试）。"""

    code = "LLM_SERVER_ERROR"


class LLMAuthError(LLMError):
    """凭据缺失 / 无效（**不可重试**：重试只会再失败一次）。"""

    code = "LLM_AUTH"


@dataclass(frozen=True)
class TextPart:
    """内容块：一段文本。"""

    text: str


@dataclass(frozen=True)
class ImagePart:
    """内容块：一张图片。

    两种来源二选一：

        url   远程地址（`https://...` / `data:` URL 也可以）
        data  原始字节 + `media_type`（适配器会编码成 data URL 发出去）

    图片支持是**模型能力**（`ModelInfo.supports_images`），由路由层校验；
    模型不收图时请求会被 `InvalidRequestError` 拒掉，而不是发给服务端才炸。
    """

    url: str | None = None
    data: bytes | None = None
    media_type: str = "image/png"
    #: OpenAI 兼容接口的细节档位：`low` / `high` / `auto`（可不给）。
    detail: str | None = None

    def __post_init__(self) -> None:
        if (self.url is None) == (self.data is None):
            raise ValueError("ImagePart 必须且只能提供 url 或 data 之一")
        if self.url is not None and not isinstance(self.url, str):
            raise TypeError(f"ImagePart.url 必须是字符串，收到：{self.url!r}")
        if self.data is not None and not isinstance(self.data, (bytes, bytearray)):
            raise TypeError(f"ImagePart.data 必须是 bytes，收到：{type(self.data)!r}")


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用（消息与流里共用）。

    `arguments` 是**未解析的 JSON 字符串**——模型流式输出的就是文本片段，
    解析失败时调用方要能拿到原始内容报错，而不是在方言层就丢掉。
    """

    id: str
    name: str
    arguments: str = "{}"

    def parsed_arguments(self) -> dict[str, Any]:
        """解析参数；不是合法 JSON 对象 → ValueError（fail loud，带原文）。"""
        import json

        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"工具 {self.name!r} 的参数不是合法 JSON：{self.arguments!r}（{exc}）"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"工具 {self.name!r} 的参数必须是 JSON 对象：{value!r}")
        return value


#: 一条消息的内容块。
ContentPart = TextPart | ImagePart
#: 消息内容：纯文本，或内容块序列（文本 + 图片混排）。
Content = str | tuple[ContentPart, ...]


@dataclass(frozen=True)
class Message:
    """一条对话消息。

    `content` 两种形态：

        "你好"                         纯文本（最常见；与旧代码兼容）
        (TextPart("看图"), ImagePart(url=...))   多模态内容块

    `tool_calls`：assistant 消息里模型发起的工具调用（可以为空）。
    `tool_call_id`：`role="tool"` 的消息回报的是哪一次调用。
    """

    role: Role
    content: Content
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            if isinstance(self.content, (bytes, bytearray)) or not isinstance(
                self.content, (tuple, list)
            ):
                raise TypeError(
                    "Message.content 必须是字符串或内容块序列，"
                    f"收到：{type(self.content)!r}"
                )
            if not self.content:
                raise ValueError("Message.content 的内容块不能为空")
            for part in self.content:
                if not isinstance(part, (TextPart, ImagePart)):
                    raise TypeError(f"未知的内容块类型：{part!r}")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("只有 assistant 消息可以携带 tool_calls")
        if self.tool_call_id is not None and self.role != "tool":
            raise ValueError("只有 tool 消息可以携带 tool_call_id")

    @property
    def text(self) -> str:
        """消息里的文本（图片部分忽略）。"""
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text for part in self.content if isinstance(part, TextPart))

    @property
    def images(self) -> tuple[ImagePart, ...]:
        """消息里的图片（纯文本消息 → 空元组）。"""
        if isinstance(self.content, str):
            return ()
        return tuple(part for part in self.content if isinstance(part, ImagePart))

    @property
    def has_images(self) -> bool:
        return bool(self.images)


@dataclass(frozen=True)
class ModelInfo:
    """一个具体模型的能力描述（**按模型**，不按 provider）。"""

    provider: str
    model: str
    context_window: int
    max_output_tokens: int | None = None
    #: 支持的推理档位（空 = 该模型/适配器不暴露这个参数）。
    reasoning_efforts: tuple[str, ...] = ()
    supports_images: bool = False
    #: 是否支持 function calling（请求里带 tools 时校验）。
    supports_tools: bool = True


@dataclass(frozen=True)
class ToolSpec:
    """给模型看的一件工具（扁平结构，与各家的 function calling 对齐）。

    `parameters` 就是工具定义里的参数表（`name → {type, required, ...}`），
    方言层负责把它翻成各家要的 `{"type": "function", "function": {...}}`。
    """

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class LLMRequest:
    """一次模型调用：**路由与调用参数都在这里**（模型选择是数据——0044）。

    字段分三类：

        路由    provider / model
        调用    messages / tools / max_tokens / reasoning_effort / temperature
        归属    session_id / user_id（用量统计按会话、按应用用户分桶——
                "日缓存命中率按应用用户独立管理"）
    """

    provider: str
    model: str
    messages: tuple[Message, ...]
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    #: 模型可见的工具（空 = 这次调用不带工具）。
    tools: tuple[ToolSpec, ...] = ()
    #: 工具选择策略：None = 适配器默认；"auto" / "none" / "required"。
    tool_choice: str | None = None
    #: 会话 id（用量统计的"本对话消耗"按它分桶；None = 不计入会话桶）。
    session_id: str | None = None
    #: 应用用户 id（"日消耗 / 日缓存命中率"按它独立管理；None = anonymous）。
    user_id: str | None = None
    #: 适配器私有的附加字段（不参与通用校验；由适配器自己解释）。
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextDelta:
    """流式文本增量。"""

    type: ClassVar[str] = "text"
    text: str


@dataclass(frozen=True)
class Usage:
    """一次调用的 token 用量（含缓存命中部分）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    #: 输入里命中缓存的部分（OpenAI 的 prompt_tokens_details.cached_tokens /
    #: DeepSeek 的 prompt_cache_hit_tokens）。命中率 = 它 / input_tokens。
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率（0.0–1.0）；没有输入 token 时为 0.0。"""
        if self.input_tokens <= 0:
            return 0.0
        return min(1.0, self.cached_input_tokens / self.input_tokens)


@dataclass(frozen=True)
class Finish:
    """流结束（**恰好一次**，永远是最后一个 chunk）。"""

    type: ClassVar[str] = "finish"
    reason: FinishReason = "stop"
    usage: Usage | None = None
    error: str | None = None
    #: 失败时的稳定 code（重试策略按它判断要不要重试）。
    error_code: str | None = None


#: 流里的分片：文本增量，或唯一的结束标记。
Chunk = TextDelta | Finish


class LLMProvider(Protocol):
    """适配器协议：提供一组模型的元信息，并能流式调用它们。

    实现方（如 `FakeProvider` / 将来的 DeepSeek 适配器）自己负责：

        · 凭据与连接配置（构造参数进，不进请求）
        · 协议方言（OpenAI 兼容 / 自定义）
        · 把异常翻成 `Finish(reason="error")` 或抛出（由调用方决定策略）
    """

    def models(self) -> Mapping[str, Any]:
        """这个适配器支持的模型：model id → 元信息（属性或字典皆可）。"""
        ...

    def stream(self, request: Any) -> Iterator[Any]:
        """流式调用；必须以一个 `type == "finish"` 的分片收尾。

        请求与分片都是**鸭子类型**：适配器插件用自己的副本，不需要 import
        本模块。分片必须带 `type` 属性（"text" / "tool_calls" / "finish"）。
        """
        ...


def assemble_text(chunks: Sequence[Chunk]) -> str:
    """把流里的文本增量拼起来（测试 / 非流式消费方的便利函数）。"""
    return "".join(
        chunk.text for chunk in chunks if getattr(chunk, "type", None) == "text"
    )


def final_usage(chunks: Sequence[Chunk]) -> Usage | None:
    """取流结束标记里的用量（没有 Finish 或没带用量 → None）。"""
    for chunk in reversed(chunks):
        if getattr(chunk, "type", None) == "finish":
            return chunk.usage
    return None


# ======================================================================
# 以下合并自 components/llm/wire.py
# ======================================================================


#: 流结束标记（OpenAI 兼容方言）。
DONE = "[DONE]"

#: 上游 finish_reason → 本项目的结束原因。
_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "stop",
}


def content_payload(content: Content) -> str | list[dict[str, Any]]:
    """消息内容 → OpenAI 兼容的 `content` 字段。

    纯文本原样给字符串；内容块序列翻译成多模态数组：

        TextPart   → {"type": "text", "text": ...}
        ImagePart  → {"type": "image_url", "image_url": {"url": ...}}

    图片两种来源：`url` 直接透传（远程地址或已是 data: URL）；`data` 按
    `media_type` 编码成 base64 data URL。
    """
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            if part.url is not None:
                url = part.url
            else:
                assert part.data is not None  # ImagePart.__post_init__ 已保证
                encoded = base64.b64encode(part.data).decode("ascii")
                url = f"data:{part.media_type};base64,{encoded}"
            image_url: dict[str, Any] = {"url": url}
            if part.detail is not None:
                image_url["detail"] = part.detail
            parts.append({"type": "image_url", "image_url": image_url})
    return parts


def message_payload(message: Message) -> dict[str, Any]:
    """一条消息 → OpenAI 兼容的消息体（含工具调用字段）。

        普通消息        {role, content}
        assistant 带调用 {role, content, tool_calls: [{id, type, function:{name, arguments}}]}
        tool 结果       {role: "tool", tool_call_id, content}
    """
    data: dict[str, Any] = {
        "role": message.role,
        "content": content_payload(message.content),
    }
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
        # OpenAI 兼容接口：带 tool_calls 的 assistant 消息，content 允许为 null。
        if not message.text:
            data["content"] = None
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def tool_payload(spec: ToolSpec) -> dict[str, Any]:
    """工具声明 → OpenAI 兼容的 function 定义。"""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": _json_schema(spec.parameters),
        },
    }


def _json_schema(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """把我们的参数表翻成 JSON Schema（OpenAI 兼容接口要的是 object schema）。

    我们的参数表是扁平的 `name → {type, required, description, enum, default}`；
    这里翻成 `{"type": "object", "properties": {...}, "required": [...]}`。
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in parameters.items():
        item = dict(spec)
        is_required = bool(item.pop("required", False))
        item.pop("default", None) if is_required else None
        properties[name] = item
        if is_required:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def build_payload(
    request: LLMRequest,
    *,
    default_params: Mapping[str, Any] | None = None,
    include_usage: bool = True,
) -> dict[str, Any]:
    """标准请求 → OpenAI 兼容的请求体。

    合并优先级（后者覆盖前者）：内置字段 → `default_params`（插件配置里的
    可定制参数）→ `request.extra`（调用方这一次的临时参数）。
    """
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [message_payload(m) for m in request.messages],
        "stream": True,
    }
    if request.tools:
        payload["tools"] = [tool_payload(spec) for spec in request.tools]
        payload["tool_choice"] = request.tool_choice or "auto"
    if include_usage:
        # 流式请求默认不带 usage，要显式要（OpenAI 兼容接口的通用开关）。
        payload["stream_options"] = {"include_usage": True}
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.reasoning_effort is not None:
        payload["reasoning_effort"] = request.reasoning_effort
    if default_params:
        payload.update(dict(default_params))
    if request.extra:
        payload.update(dict(request.extra))
    return payload


def parse_data_line(line: str) -> dict[str, Any] | None:
    """解析一行 SSE：`data: {...}` → dict；心跳 / 非 data 行 → None；[DONE] → None。

    `[DONE]` 由调用方通过 `is_done` 判断——这里返回 None 是为了不把控制流藏在
    数据解析里。
    """
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[len("data:") :].strip()
    if not data or data == DONE:
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def is_done(line: str) -> bool:
    """这一行是不是结束标记 `data: [DONE]`。"""
    text = line.strip()
    return text.startswith("data:") and text[len("data:") :].strip() == DONE


def text_delta(event: Mapping[str, Any]) -> str | None:
    """从一条流事件里取文本增量（没有内容 → None）。"""
    for choice in event.get("choices") or ():
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            return content
    return None


def finish_reason(event: Mapping[str, Any]) -> FinishReason | None:
    """从一条流事件里取结束原因（还没结束 → None）。"""
    for choice in event.get("choices") or ():
        if not isinstance(choice, Mapping):
            continue
        raw = choice.get("finish_reason")
        if raw:
            return _FINISH_REASONS.get(str(raw), "stop")
    return None


def usage_from_event(event: Mapping[str, Any]) -> Usage | None:
    """从一条流事件里取用量（没有 usage 字段 → None）。

    兼容两种缓存字段：

        OpenAI          prompt_tokens_details.cached_tokens
        DeepSeek        prompt_cache_hit_tokens
    """
    raw = event.get("usage")
    if not isinstance(raw, Mapping):
        return None
    input_tokens = int(raw.get("prompt_tokens") or 0)
    output_tokens = int(raw.get("completion_tokens") or 0)
    details = raw.get("prompt_tokens_details")
    cached = (
        int(details.get("cached_tokens") or 0)
        if isinstance(details, Mapping)
        else int(raw.get("prompt_cache_hit_tokens") or 0)
    )
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
    )


def tool_call_deltas(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从一条流事件里取出 tool_calls 的**增量分片**。

    OpenAI 兼容接口的流式工具调用长这样（一个调用会被拆成多条）：

        {"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": ""}}
        {"index": 0, "function": {"arguments": "{\\"path\\":"}}
        {"index": 0, "function": {"arguments": "\\"a.txt\\"}"}}

    所以这里返回**原始分片**，由 `ToolCallAccumulator` 按 index 拼接。
    """
    deltas: list[dict[str, Any]] = []
    for choice in event.get("choices") or ():
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta") or {}
        for item in delta.get("tool_calls") or ():
            if isinstance(item, Mapping):
                deltas.append(dict(item))
    return deltas


class ToolCallAccumulator:
    """按 index 拼接流式工具调用分片，结束时产出完整的 `ToolCall`。"""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, deltas: Iterable[Mapping[str, Any]]) -> None:
        for delta in deltas:
            index = int(delta.get("index", 0))
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if delta.get("id"):
                slot["id"] = str(delta["id"])
            function = delta.get("function")
            if isinstance(function, Mapping):
                if function.get("name"):
                    slot["name"] = str(function["name"])
                if function.get("arguments"):
                    slot["arguments"] += str(function["arguments"])

    def calls(self) -> tuple[ToolCall, ...]:
        """按 index 顺序产出完整调用；没有 id 的补一个稳定 id。"""
        out: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            name = slot["name"]
            if not name:
                continue  # 只有空分片：不是有效调用
            call_id = slot["id"] or f"call_{index}"
            out.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=slot["arguments"] or "{}",
                )
            )
        return tuple(out)

    def __bool__(self) -> bool:
        return bool(self._calls)


#: 流里新增的两种分片：工具调用开始 / 完成（文本与结束标记保持原样）。
@dataclass(frozen=True)
class ToolCallsDelta:
    """一次或多次工具调用的**增量**（消费方自己聚合，或交给 `parse_stream`）。"""

    type: ClassVar[str] = "tool_calls"
    deltas: tuple[Mapping[str, Any], ...]


def parse_stream(lines: Iterable[str]) -> Iterable[object]:
    """把 SSE 行流翻译成 `TextDelta` / `ToolCallsDelta` + 唯一的 `Finish`。

    - `data: [DONE]` 结束；
    - 事件里的 `error` 字段 → 抛 `LLMError`（带 code）；
    - 工具调用分片以 `ToolCallsDelta` 原样透出（聚合由 `ToolCallAccumulator`
      做——消费方既可以是 loop，也可以是测试）；
    - 没有 finish_reason 也在流结束时补一个 `Finish(reason="stop")`。
    """
    reason: FinishReason | None = None
    usage: Usage | None = None
    for line in lines:
        if is_done(line):
            break
        event = parse_data_line(line)
        if event is None:
            continue
        error = event.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or error.get("type") or "LLM_SERVER_ERROR")
            message = str(error.get("message") or error)
            raise LLMError(f"{code}: {message}")
        text = text_delta(event)
        if text is not None:
            yield TextDelta(text)
        deltas = tool_call_deltas(event)
        if deltas:
            yield ToolCallsDelta(deltas=tuple(deltas))
        found_reason = finish_reason(event)
        if found_reason is not None:
            reason = found_reason
        found_usage = usage_from_event(event)
        if found_usage is not None:
            usage = found_usage
    yield Finish(reason=reason or "stop", usage=usage)
