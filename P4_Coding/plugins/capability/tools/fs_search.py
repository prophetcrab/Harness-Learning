"""tool-fs-search —— 检索工具（能力插件）：glob 与 grep。

**独立于文件工具**：搜索是"遍历 + 匹配"的工作流，不是文件系统服务的通用 API。
它只复用 fs 的边界与读取策略（列目录 / 读文件都走 fs 服务）。

    glob(pattern, path=".")       按通配符找文件（结果限制在工作区内）
    grep(pattern, path=".", include="*")   正则搜索文件内容，返回行号

结果有上限（默认 200 条），避免把整个仓库灌进上下文。
"""

from __future__ import annotations

import fnmatch
import inspect
import re
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
FS_SERVICE = "fs"
TOOL_ADMIN_SERVICE = "tool-admin"
WORKSPACE_SERVICE = "workspace"



#: 单次搜索的结果上限。
DEFAULT_LIMIT = 200

class ToolFsSearchPlugin:
    name = "tool-fs-search"
    inject = (FS_SERVICE, WORKSPACE_SERVICE, TOOL_ADMIN_SERVICE)

    def __init__(self, *, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = int(limit)

    def setup(self, ctx: Context):
        fs: FileSystemService = ctx.require(FS_SERVICE)
        workspace: Workspace = ctx.require(WORKSPACE_SERVICE)
        tools = ctx.require(TOOL_ADMIN_SERVICE)

        @tool("glob")
        def glob(pattern: str, path: str = ".") -> dict[str, Any]:
            """按通配符查找文件（如 "**/*.py"）。

            参数 pattern：glob 模式。path：从哪个目录开始（工作区内）。
            """
            base = fs.workspace.resolve(path)
            if not base.is_dir():
                raise ValueError(f"不是目录：{path!r}")
            matches: list[str] = []
            for target in base.glob(pattern):
                if not workspace.contains(target):
                    continue  # 模式里带 .. 也逃不出去
                if target.is_file():
                    matches.append(target.relative_to(workspace.root).as_posix())
                if len(matches) >= self._limit:
                    break
            return {"pattern": pattern, "path": path, "matches": sorted(matches), "truncated": len(matches) >= self._limit}

        @tool("grep")
        def grep(pattern: str, path: str = ".", include: str = "*") -> dict[str, Any]:
            """在文件内容里做正则搜索，返回命中的文件、行号与整行文本。

            参数 pattern：正则。path：搜索目录（工作区内）。
            include：只搜这些文件名（通配符，默认全部）。
            """
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"正则表达式不合法：{exc}") from exc
            hits: list[dict[str, Any]] = []
            for entry in fs.list_dir(path, recursive=True):
                if entry["type"] != "file":
                    continue
                if not fnmatch.fnmatch(entry["path"].rsplit("/", 1)[-1], include):
                    continue
                try:
                    text = fs.read_text(entry["path"])
                except Exception:  # noqa: BLE001 —— 二进制 / 不可读的文件跳过
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        hits.append({"path": entry["path"], "line": number, "text": line})
                        if len(hits) >= self._limit:
                            return {
                                "pattern": pattern,
                                "hits": hits,
                                "truncated": True,
                            }
            return {"pattern": pattern, "hits": hits, "truncated": False}

        handles = [
            tools.register(glob.definition, glob.func, owner=self.name),
            tools.register(grep.definition, grep.func, owner=self.name),
        ]

        def dispose() -> None:
            for handle in reversed(handles):
                handle()

        return dispose

def tool_fs_search_plugin(**config: Any) -> ToolFsSearchPlugin:
    return ToolFsSearchPlugin(**config)

__all__ = ["DEFAULT_LIMIT", "ToolFsSearchPlugin", "tool_fs_search_plugin"]

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
Workspace = Any
