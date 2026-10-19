"""tool-shell —— 命令执行工具（能力插件）。

模型只看到 `shell(command, timeout)`；执行细节全在 `proc` 服务里：

    · Windows 默认 Git Bash（POSIX 语法），POSIX 默认 sh；
    · 方言写错（cmd / PowerShell）→ 拒绝并给纠正提示，不执行；
    · 超时杀整棵进程树，结果带已经拿到的部分输出；
    · 输出 UTF-8 优先、GBK 兜底。

**需要审批**（`needs_approval=True`）：这条路径能执行任意命令，是风险最高
的工具。没有审批通道时 fail-closed。
"""

from __future__ import annotations

import inspect
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
PROC_SERVICE = "proc"
TOOL_ADMIN_SERVICE = "tool-admin"
WORKSPACE_SERVICE = "workspace"



#: 默认提示词说明（工具在时才显示）。
SHELL_SECTION = "tool:shell"
SHELL_HINT = (
    "shell 工具：在 Git Bash（POSIX 语法）里执行命令。"
    "路径用 /d/project/... 或相对路径；不要用 cmd 语法（dir / copy / del）"
    "或 PowerShell 语法（Get-ChildItem / $env:VAR）。"
    "读文件优先用 read_file，不要用 cat。"

)
class ToolShellPlugin:
    name = "tool-shell"
    inject = (PROC_SERVICE, WORKSPACE_SERVICE, TOOL_ADMIN_SERVICE)
    optional = ("prompt",)

    def __init__(self, *, default_timeout: float = 30.0, idle_timeout: float | None = None) -> None:
        self._default_timeout = default_timeout
        self._idle_timeout = idle_timeout

    def setup(self, ctx: Context):
        proc: ProcRuntime = ctx.require(PROC_SERVICE)
        workspace: Workspace = ctx.require(WORKSPACE_SERVICE)
        tools = ctx.require(TOOL_ADMIN_SERVICE)

        @tool("shell", needs_approval=True)
        def shell(command: str, timeout: float | None = None) -> ToolResult:
            """在工作区里执行一条 shell 命令（Git Bash / POSIX 语法）。

            参数 command：要执行的命令。timeout：超时秒数（默认 30）。
            """
            result: ProcResult = proc.run(
                command,
                cwd=workspace.root,
                timeout=timeout if timeout is not None else self._default_timeout,
                idle_timeout=self._idle_timeout,
            # 直接返回 ToolResult：proc 的 error/timeout **不能**被包装成 ok
            # （方言写错、命令失败都要让模型看见）。
            )
            return ToolResult(
                status=result.status,  # type: ignore[arg-type]
                content=result.as_dict(),
                message=result.stderr.strip(),
                error_code="SHELL_FAILED" if result.status == "error" else None,

            )
        off_tool = tools.register(shell.definition, shell.func, owner=self.name)
        prompt = ctx.get("prompt")
        off_section = None
        if prompt is not None:
            off_section = prompt.section(
                SHELL_SECTION,
                lambda context: SHELL_HINT,
                order=210,
            )
        def dispose() -> None:
            if off_section is not None:
                off_section()
            off_tool()

        return dispose

def tool_shell_plugin(**config: Any) -> ToolShellPlugin:
    return ToolShellPlugin(**config)

__all__ = ["SHELL_HINT", "SHELL_SECTION", "ToolShellPlugin", "tool_shell_plugin"]

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
ProcResult = Any
ProcRuntime = Any
Workspace = Any
