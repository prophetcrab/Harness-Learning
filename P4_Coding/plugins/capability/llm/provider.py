"""llm-provider —— 通用适配器：方言翻译 + 连接 + 可定制参数（能力子插件）。

它把"标准请求"翻译成某家 API 的线格式，再把回包翻译回 `Chunk`。当前方言是
**OpenAI 兼容**（DeepSeek 也走这套，缓存字段两种都认）。加一家供应商 =
加一个方言函数 + 一条适配器配置。

**配置（装配单里的一行）**：

    {
      "id": "llm-deepseek",
      "name": "llm-provider",
      "role": "capability",
      "config": {
        "route": "deepseek",                     # 路由名（请求里按它选）
        "base_url": "https://api.deepseek.com/chat/completions",
        "api_key_env": "DEEPSEEK_API_KEY",       # 密钥不进装配单
        "models": [
          {"id": "deepseek-chat", "context_window": 128000,
           "max_output_tokens": 8192}
        ],
        "default_params": {"top_p": 0.9},        # 可定制：并进每个请求体
        "timeout": 60
      }
    }

**可定制化参数**三层合并（后者覆盖前者）：内置字段 → `default_params` →
`request.extra`（某一次调用临时加）。

**连接与方言分离**：`transport` 是可注入的（测试用假传输；默认 urllib）。
适配器自己不重试——那是 `llm-retry` 的事。

类型与纯函数（协议、方言、流解析）复用 `plugins.base.llm` 的公开定义——
能力插件依赖基座插件是单向的，不形成环（决策 0052）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from core import Context
from plugins.base.llm import (
    LLM_SERVICE,
    Chunk,
    LLMAuthError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRequest,
    LLMServerError,
    LLMTimeoutError,
    ModelInfo,
    build_payload,
    parse_stream,
)

#: 传输协议：`(url, headers, payload, timeout) -> 逐行 SSE`。
Transport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Iterable[str]]


def _normalize_endpoint(base_url: str) -> str:
    """`base_url` 可以是服务根地址，也可以已经是完整端点（如 `/chat`）。

    只有**看起来还是根地址**时才补 `/chat/completions`：

        https://api.deepseek.com          → .../chat/completions
        https://api.openai.com/v1         → .../v1/chat/completions
        https://api.example/chat          → 原样（已经是端点路径）
        https://api.example/chat/completions → 原样
    """
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/chat/completions"):
        return endpoint
    path = urllib.parse.urlparse(endpoint).path
    if path in ("", "/") or path.endswith("/v1"):
        return f"{endpoint}/chat/completions"
    return endpoint


def http_transport(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> Iterable[str]:
    """默认传输：POST JSON、按行读 SSE。把 HTTP 错误翻成带 code 的异常。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={**dict(headers), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw in response:
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001 —— 读错误体失败不影响分类
            pass
        status = exc.code
        if status in (401, 403):
            raise LLMAuthError(f"凭据被拒绝（HTTP {status}）：{detail}") from exc
        if status == 429:
            raise LLMRateLimitError(f"被限流（HTTP 429）：{detail}") from exc
        if status >= 500:
            raise LLMServerError(f"上游错误（HTTP {status}）：{detail}") from exc
        raise LLMConnectionError(f"请求失败（HTTP {status}）：{detail}") from exc
    except TimeoutError as exc:
        raise LLMTimeoutError(f"请求超时（{timeout}s）") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise LLMTimeoutError(f"请求超时（{timeout}s）") from exc
        raise LLMConnectionError(f"连接失败：{reason}") from exc


class OpenAICompatibleProvider:
    """OpenAI 兼容方言的适配器（DeepSeek / 各种兼容端点都能用）。"""

    def __init__(
        self,
        *,
        route: str,
        base_url: str,
        models: Sequence[Mapping[str, Any]],
        api_key_env: str | None = None,
        api_key: str | None = None,
        default_params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        include_usage: bool = True,
        transport: Transport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("llm-provider 需要 base_url（端点 URL 或服务根地址）")
        self._route = route
        self._base_url = _normalize_endpoint(base_url)
        self._api_key_env = api_key_env
        self._api_key = api_key
        self._default_params = dict(default_params or {})
        self._extra_headers = dict(extra_headers or {})
        self._timeout = float(timeout)
        self._include_usage = include_usage
        self._transport = transport or http_transport
        self._models = _parse_models(route, models)

    @property
    def route(self) -> str:
        return self._route

    def models(self) -> Mapping[str, ModelInfo]:
        return dict(self._models)

    def stream(self, request: LLMRequest) -> Iterable[Chunk]:
        payload = build_payload(
            request,
            default_params=self._default_params,
            include_usage=self._include_usage,
        )
        headers: dict[str, str] = {}
        key = self._api_key or (
            os.environ.get(self._api_key_env) if self._api_key_env else None
        )
        if self._api_key_env and not key:
            raise LLMAuthError(
                f"缺少凭据：环境变量 {self._api_key_env!r} 未设置"
                "（密钥不进装配单，用环境变量）"
            )
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self._extra_headers)
        lines = self._transport(self._base_url, headers, payload, self._timeout)
        yield from parse_stream(lines)


def _parse_models(
    route: str, models: Sequence[Mapping[str, Any]]
) -> dict[str, ModelInfo]:
    """把配置里的模型列表变成 ModelInfo（缺 id / 空列表都 fail loud）。"""
    if isinstance(models, (str, bytes)) or not isinstance(models, Sequence):
        raise ValueError("llm-provider 的 models 必须是模型配置的列表")
    if not models:
        raise ValueError("llm-provider 至少要配置一个模型")
    out: dict[str, ModelInfo] = {}
    for item in models:
        if not isinstance(item, Mapping):
            raise ValueError(f"模型配置必须是字典：{item!r}")
        model_id = item.get("id") or item.get("model")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError(f"模型配置缺少 id：{dict(item)!r}")
        if model_id in out:
            raise ValueError(f"模型 id 重复：{model_id!r}")
        efforts = item.get("reasoning_efforts") or ()
        out[model_id] = ModelInfo(
            provider=route,
            model=model_id,
            context_window=int(item.get("context_window") or 0),
            max_output_tokens=(
                int(item["max_output_tokens"])
                if item.get("max_output_tokens") is not None
                else None
            ),
            reasoning_efforts=tuple(str(effort) for effort in efforts),
            supports_images=bool(item.get("supports_images", False)),
            supports_tools=bool(item.get("supports_tools", True)),
        )
    return out


class LLMProviderPlugin:
    """注册一条路由（插件名带路由后缀：一个进程里可以挂多条 provider）。"""

    def __init__(
        self,
        *,
        route: str = "default",
        base_url: str,
        models: Sequence[Mapping[str, Any]],
        api_key_env: str | None = None,
        api_key: str | None = None,
        default_params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        include_usage: bool = True,
        transport: Transport | None = None,
    ) -> None:
        self.name = f"llm-provider:{route}"
        self._provider = OpenAICompatibleProvider(
            route=route,
            base_url=base_url,
            models=models,
            api_key_env=api_key_env,
            api_key=api_key,
            default_params=default_params,
            extra_headers=extra_headers,
            timeout=timeout,
            include_usage=include_usage,
            transport=transport,
        )

    def setup(self, ctx: Context):
        runtime = ctx.require(LLM_SERVICE)
        # 返回注销句柄：适配器卸载时路由随之消失（注册即回卷）。
        return runtime.register(self._provider.route, self._provider)


def llm_provider_plugin(**config: Any) -> LLMProviderPlugin:
    """工厂：装配单的 `config` 直接喂给构造参数。"""
    return LLMProviderPlugin(**config)


__all__ = [
    "LLMProviderPlugin",
    "OpenAICompatibleProvider",
    "Transport",
    "http_transport",
    "llm_provider_plugin",
]
