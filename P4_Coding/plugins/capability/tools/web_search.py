"""tool-web-search —— 联网工具（能力插件）：web_fetch 与 web_search。

**两种能力分开对待**：

    web_fetch  抓一个 URL 的文本（默认 urllib；可注入 transport 供测试）
    web_search 需要一个搜索端点（SearXNG 兼容的 JSON 接口）；没配置时
               返回明确的 `WEB_SEARCH_NOT_CONFIGURED`，而不是假装没结果

**安全**：只接受 http/https；响应体积有上限；不做任何凭据转发。
"""

from __future__ import annotations

import inspect
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core import Context


# === 本地副本（来自 plugins/base/tools_admin.py；插件之间不 import，见决策 0053）===
@dataclass(frozen=True)
class ParamSpec:
    """一个参数：类型 / 是否必填 / 默认值 / 枚举 / 说明。"""

    type: ParamType = "string"
    required: bool = False
    description: str = ""
    default: Any = None
    enum: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }
        if self.enum:
            data["enum"] = list(self.enum)
        if not self.required and self.default is not None:
            data["default"] = self.default
        return data

ParamType = Literal["string", "integer", "number", "boolean", "object", "array"]

@dataclass(frozen=True)
class ToolDefinition:
    """一件工具的声明（模型看到的只有 name / description / parameters）。"""

    name: str
    description: str
    parameters: Mapping[str, ParamSpec] = field(default_factory=dict)
    #: 是否需要人工审批（管线在 pre-execute 阶段据此发 ask 决策）。
    needs_approval: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {key: spec.as_dict() for key, spec in self.parameters.items()},
            "needs_approval": self.needs_approval,
        }

@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果（管线统一产出这种形状）。"""

    status: ToolStatus
    content: Any = None
    message: str = ""
    duration_ms: int = 0
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "content": self.content,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }

@dataclass(frozen=True)
class _Tool:
    """`@tool` 的返回值：定义 + 实现。"""

    definition: ToolDefinition
    func: Callable[..., Any]


def tool(
    name: str,
    *,
    description: str | None = None,
    needs_approval: bool = False,
) -> Callable[[Callable[..., Any]], _Tool]:
    """把函数声明成工具：schema 从签名推导，说明默认取 docstring 第一段。"""

    def decorate(func: Callable[..., Any]) -> _Tool:
        parameters: dict[str, ParamSpec] = {}
        for pname, param in inspect.signature(func).parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            required = param.default is inspect.Parameter.empty
            parameters[pname] = ParamSpec(
                type=_param_type(param.annotation),
                required=required,
                default=None if required else param.default,
            )
        doc = (description or inspect.getdoc(func) or "").strip()
        return _Tool(
            definition=ToolDefinition(
                name=name,
                description=doc,
                parameters=parameters,
                needs_approval=needs_approval,
            ),
            func=func,

        )
    return decorate

def validate_arguments(definition: ToolDefinition, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """校验并补齐参数；错误 → ValueError（带工具名与参数名）。"""
    if not isinstance(arguments, Mapping):
        raise ValueError(f"工具 {definition.name!r} 的参数必须是字典")
    known = set(definition.parameters)
    unknown = sorted(set(arguments) - known)
    if unknown:
        raise ValueError(
            f"工具 {definition.name!r} 不认识参数：{unknown}（可用：{sorted(known)}）"
        )
    out: dict[str, Any] = {}
    for pname, spec in definition.parameters.items():
        if pname in arguments:
            value = arguments[pname]
            _check_type(definition.name, pname, spec, value)
            if spec.enum and value not in spec.enum:
                raise ValueError(
                    f"工具 {definition.name!r} 的参数 {pname!r} 必须是 "
                    f"{list(spec.enum)} 之一，收到：{value!r}"
                )
            out[pname] = value
        elif spec.required:
            raise ValueError(f"工具 {definition.name!r} 缺少必填参数：{pname!r}")
        else:
            out[pname] = spec.default
    return out

_TYPE_CHECKS: dict[ParamType, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


def _check_type(tool_name: str, pname: str, spec: ParamSpec, value: Any) -> None:
    # bool 是 int 的子类：integer 不接受 True/False（模型的常见错误）。
    if spec.type == "integer" and isinstance(value, bool):
        raise ValueError(f"工具 {tool_name!r} 的参数 {pname!r} 必须是整数，收到布尔值")
    expected = _TYPE_CHECKS[spec.type]
    if not isinstance(value, expected):
        raise ValueError(
            f"工具 {tool_name!r} 的参数 {pname!r} 类型不对："
            f"需要 {spec.type}，收到 {type(value).__name__}"
        )


# --- 服务/事件名（字符串，本地复刻）---
TOOL_ADMIN_SERVICE = "tool-admin"



#: 抓取传输：`(url, timeout) -> str`。
FetchText = Callable[[str, float], str]

class WebError(RuntimeError):
    code = "WEB_ERROR"

class WebNotConfigured(WebError):
    code = "WEB_SEARCH_NOT_CONFIGURED"

def urllib_transport(url: str, timeout: float) -> str:
    """默认传输：GET 一个 URL，按 UTF-8 解码。"""
    request = urllib.request.Request(url, headers={"User-Agent": "p4-harness/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise WebError(f"HTTP {exc.code}：{url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WebError(f"请求失败：{url}（{exc}）") from exc

def _check_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError(f"只支持 http/https：{url!r}")

class ToolWebSearchPlugin:
    name = "tool-web-search"
    inject = (TOOL_ADMIN_SERVICE,)

    def __init__(
        self,
        *,
        search_url: str | None = None,
        timeout: float = 15.0,
        max_chars: int = 20_000,
        transport: FetchText | None = None,
    ) -> None:
        self._search_url = search_url
        self._timeout = float(timeout)
        self._max_chars = int(max_chars)
        self._transport = transport or urllib_transport

    def setup(self, ctx: Context):
        tools = ctx.require(TOOL_ADMIN_SERVICE)

        @tool("web_fetch")
        def web_fetch(url: str) -> dict[str, Any]:
            """抓取一个网页的文本内容（只支持 http/https）。

            参数 url：完整地址。
            """
            _check_url(url)
            text = self._transport(url, self._timeout)
            truncated = len(text) > self._max_chars
            return {
                "url": url,
                "content": text[: self._max_chars],
                "truncated": truncated,
            }

        @tool("web_search")
        def web_search(query: str, limit: int = 5) -> dict[str, Any]:
            """搜索网页，返回标题 / 链接 / 摘要。

            参数 query：搜索词。limit：最多返回几条（默认 5）。
            """
            if not self._search_url:
                raise WebNotConfigured(
                    "没有配置搜索端点（装配单里给 tool-web-search 的 search_url）"
                )
            url = self._search_url
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
            body = self._transport(url, self._timeout)
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise WebError(f"搜索端点返回的不是 JSON：{exc}") from exc
            raw_results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(raw_results, list):
                raise WebError("搜索端点返回里没有 results 列表")
            results = []
            for item in raw_results[: max(1, int(limit))]:
                if not isinstance(item, dict):
                    continue
                results.append(
                    {
                        "title": str(item.get("title", "")),
                        "url": str(item.get("url", "")),
                        "snippet": str(item.get("content", "")),
                    }
                )
            return {"query": query, "results": results}

        handles = [
            tools.register(web_fetch.definition, web_fetch.func, owner=self.name),
            tools.register(web_search.definition, web_search.func, owner=self.name),
        ]

        def dispose() -> None:
            for handle in reversed(handles):
                handle()

        return dispose

def tool_web_search_plugin(**config: Any) -> ToolWebSearchPlugin:
    return ToolWebSearchPlugin(**config)

__all__ = [
    "FetchText",
    "ToolWebSearchPlugin",
    "WebError",
    "WebNotConfigured",
    "tool_web_search_plugin",
    "urllib_transport",
]

# === 本地副本（来自 plugins/base/tools_admin.py）===
_TYPE_BY_NAME: dict[str, ParamType] = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "Any": "string",
}


def _param_type(annotation: Any) -> ParamType:
    if annotation is inspect.Parameter.empty:
        return "string"
    if isinstance(annotation, str):
        # 去掉可选标记与泛型参数，取基础类型名：
        #   "float | None" -> float; "list[str]" -> list; "dict[str, Any]" -> dict
        text = annotation
        for union in ("| None", "None |", "Optional[", "|"):
            text = text.replace(union, " ")
        text = text.strip()
        head = text.split("[", 1)[0].strip().split()[0] if text.strip() else ""
        return _TYPE_BY_NAME.get(head, "string")
    if annotation is str:
        return "string"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is dict:
        return "object"
    if annotation is list:
        return "array"
    # 泛型：list[str] / dict[str, Any] / X | None
    origin = getattr(annotation, "__origin__", None)
    if origin is list or origin is tuple:
        return "array"
    if origin is dict:
        return "object"
    args = getattr(annotation, "__args__", ())
    if args:
        # X | None → 取非 None 的那一支
        for item in args:
            if item is type(None):
                continue
            return _param_type(item)
    return "string"

# --- 本地类型别名 ---
ToolStatus = str
