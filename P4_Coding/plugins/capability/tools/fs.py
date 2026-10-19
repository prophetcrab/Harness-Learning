"""tool-fs —— 文件工具（能力插件）：read_file / list_files / write_file / edit_file。

**它不做 I/O**：所有读写都走 `fs` 服务（边界 + 沙箱 + 读后写策略都在那里）。
这层只负责：模型参数 → 服务调用 → 返回结构化结果。

审批由 `fs-sandbox` 决定，所以这里的 `needs_approval` 都是 False——
如果在工具层也标一遍，同一次写会被问两次。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core import Context
from plugins.base.tools_admin import current_session_id


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
FS_SERVICE = "fs"
TOOL_ADMIN_SERVICE = "tool-admin"



def _read(
    fs: FileSystemService,
    path: str,
    offset: int,
    limit: int,
    session_id: str | None,
) -> dict[str, Any]:
    text = fs.read_text(path, session_id=session_id)
    lines = text.splitlines()
    start = max(0, offset - 1)
    window = lines[start : start + max(0, limit)]
    return {
        "path": path,
        "offset": offset,
        "lines": [{"number": start + index + 1, "text": line} for index, line in enumerate(window)],
        "total_lines": len(lines),
        "truncated": start + len(window) < len(lines),
    }

class ToolFsPlugin:
    """注册四件文件工具。"""

    name = "tool-fs"
    inject = (FS_SERVICE, TOOL_ADMIN_SERVICE)

    def setup(self, ctx: Context):
        fs: FileSystemService = ctx.require(FS_SERVICE)
        tools = ctx.require(TOOL_ADMIN_SERVICE)

        @tool("read_file")
        def read_file(path: str, offset: int = 1, limit: int = 200) -> dict[str, Any]:
            """读取一个 UTF-8 文本文件（带行号，可分页）。

            参数 path：工作区内的相对路径。offset：从第几行开始（1 起）。
            limit：最多返回多少行。
            """
            return _read(fs, path, offset, limit, current_session_id())

        @tool("list_files")
        def list_files(path: str = ".", recursive: bool = False) -> list[dict[str, Any]]:
            """列出目录内容（路径、类型、大小）。

            参数 path：工作区内的相对路径。recursive：是否递归列出子目录。
            """
            return fs.list_dir(
                path, recursive=recursive, session_id=current_session_id()
            )

        @tool("write_file")
        def write_file(path: str, content: str) -> dict[str, Any]:
            """把一个字符串完整写入文件（覆盖写）。

            参数 path：工作区内的相对路径。content：完整文件内容。
            """
            return fs.write_text(path, content, session_id=current_session_id())

        @tool("edit_file")
        def edit_file(path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
            """把文件里的旧文本替换成新文本（默认只替换第一处）。

            参数 path：工作区内的相对路径。old：要替换的原文。new：替换后的文本。
            count：最多替换几处（0 = 全部）。
            """
            return fs.edit_text(
                path, old, new, count=count, session_id=current_session_id()
            )

        handles = [
            tools.register(read_file.definition, read_file.func, owner=self.name),
            tools.register(list_files.definition, list_files.func, owner=self.name),
            tools.register(write_file.definition, write_file.func, owner=self.name),
            tools.register(edit_file.definition, edit_file.func, owner=self.name),
        ]

        def dispose() -> None:
            for handle in reversed(handles):
                handle()

        return dispose

def tool_fs_plugin(**config: Any) -> ToolFsPlugin:
    return ToolFsPlugin(**config)

__all__ = ["ToolFsPlugin", "tool_fs_plugin"]

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
FileSystemService = Any
