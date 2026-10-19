"""tool-ask-user —— 向使用者提问（能力插件）。

模型卡在歧义上时应该**问人**，而不是猜。工具的约定是用户要求的：

    · 每个提问至少给两个选项；
    · 默认允许"自定义输入"（选项之外的回答）；
    · 提问通道由装配方注入（终端 / 网页 / 测试），没有通道时 **fail loud**
      ——不会静默返回一个假答案。

服务 `ask-user` 暴露 `ask(question, options, allow_custom)`；工具只是它的
薄包装（与 P3 的"三件工具是服务的薄包装"同一形状）。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
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



#: 提问服务的名字。
ASK_USER_SERVICE = "ask-user"

#: 提问通道：`(question, options, allow_custom) -> answer`。
Asker = Callable[[str, tuple[str, ...], bool], str]

class AskUnavailable(RuntimeError):
    """没有可用的提问通道（fail loud：不编造答案）。"""

    code = "ASK_NO_CHANNEL"

class AskUserService:
    """提问通道的注册表 + 问题校验。"""

    def __init__(self, asker: Asker | None = None) -> None:
        self._asker = asker

    def set_asker(self, asker: Asker | None) -> None:
        self._asker = asker

    @property
    def available(self) -> bool:
        return self._asker is not None

    def ask(
        self, question: str, options: Sequence[str], *, allow_custom: bool = True
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("问题不能为空")
        cleaned = tuple(str(option).strip() for option in options)
        if len(cleaned) < 2:
            raise ValueError("每个提问至少要给两个选项")
        if any(not option for option in cleaned):
            raise ValueError("选项不能是空字符串")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("选项不能重复")
        if self._asker is None:
            raise AskUnavailable(
                "没有可用的提问通道（装配方需要给 tool-ask-user 注入一个 asker）"
            )
        answer = self._asker(question, cleaned, allow_custom)
        return {
            "question": question,
            "options": list(cleaned),
            "allow_custom": allow_custom,
            "answer": answer,
        }

class ToolAskUserPlugin:
    name = "tool-ask-user"
    inject = (TOOL_ADMIN_SERVICE,)

    def __init__(self, *, asker: Asker | None = None) -> None:
        self._asker = asker

    def setup(self, ctx: Context):
        tools = ctx.require(TOOL_ADMIN_SERVICE)
        service = AskUserService(self._asker)
        ctx.provide(ASK_USER_SERVICE, service)

        @tool("ask_user")
        def ask_user(
            question: str, options: list[str] | None = None, allow_custom: bool = True
        ) -> dict[str, Any]:
            """向使用者提问（至少两个选项；默认允许自定义回答）。

            参数 question：要问的问题。options：候选项（至少两个）。
            allow_custom：是否允许选项之外的自定义回答。
            """
            return service.ask(question, list(options or []), allow_custom=allow_custom)

        off_tool = tools.register(ask_user.definition, ask_user.func, owner=self.name)

        def dispose() -> None:
            off_tool()
            ctx.retract(ASK_USER_SERVICE)

        return dispose

def tool_ask_user_plugin(**config: Any) -> ToolAskUserPlugin:
    return ToolAskUserPlugin(**config)

__all__ = [
    "ASK_USER_SERVICE",
    "AskUnavailable",
    "AskUserService",
    "Asker",
    "ToolAskUserPlugin",
    "tool_ask_user_plugin",
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
