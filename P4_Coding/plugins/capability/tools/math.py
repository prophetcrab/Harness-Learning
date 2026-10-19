"""tool-math —— 计算工具（能力插件）：`calc` 与 `math`。

`calc`：安全的四则运算 / 括号 / 幂 / 常用函数，用 AST 白名单求值——**不用
`eval`**（模型生成的表达式不能有代码执行能力）。

`math`：高级数学函数（sqrt / log / trig / factorial / gcd…），参数是一个
函数名 + 数字列表。

**插件边界**（决策 0053）：本文件不认识 `tool-admin` 插件，只认服务名
`"tool-admin"` 与工具定义的**数据形状**。`@tool` 装饰器与 `ToolDefinition`
是下面的本地副本——刻意与其他插件各存一份，不做跨插件 import。
"""

from __future__ import annotations

import ast
import inspect
import math
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core import Context

TOOL_ADMIN_SERVICE = "tool-admin"


# ---------------------------------------------------------------------------
# 本地副本：工具定义与 `@tool`（来自 plugins/base/tools_admin.py）
# ---------------------------------------------------------------------------

ParamType = Literal["string", "integer", "number", "boolean", "object", "array"]
ToolStatus = Literal["ok", "error", "denied", "timeout"]


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


@dataclass(frozen=True)
class ToolDefinition:
    """一件工具的声明（模型看到的只有 name / description / parameters）。"""

    name: str
    description: str
    parameters: Mapping[str, ParamSpec] = field(default_factory=dict)
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
    """一次工具调用的结果（管线按属性读取，不要求同一个类）。"""

    status: ToolStatus
    content: Any = None
    message: str = ""
    duration_ms: int = 0
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class _Tool:
    definition: ToolDefinition
    func: Callable[..., Any]


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
        text = annotation
        for union in ("| None", "None |", "Optional[", "|"):
            text = text.replace(union, " ")
        text = text.strip()
        head = text.split("[", 1)[0].strip().split()[0] if text.strip() else ""
        return _TYPE_BY_NAME.get(head, "string")
    if annotation in (str, int, float, bool, dict, list):
        return {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            dict: "object",
            list: "array",
        }[annotation]  # type: ignore[return-value]
    origin = getattr(annotation, "__origin__", None)
    if origin in (list, tuple):
        return "array"
    if origin is dict:
        return "object"
    args = getattr(annotation, "__args__", ())
    if args:
        for item in args:
            if item is not type(None):
                return _param_type(item)
    return "string"


def tool(
    name: str,
    *,
    description: str | None = None,
    needs_approval: bool = False,
) -> Callable[[Callable[..., Any]], _Tool]:
    """把函数声明成工具：schema 从签名推导，说明默认取 docstring。"""

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


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}

_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "degrees": math.degrees,
    "radians": math.radians,
    "hypot": math.hypot,
}

_MATH_NAMES = tuple(sorted(_FUNCTIONS))


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"不支持的常量：{node.value!r}")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"未知标识符：{node.id!r}（可用常数：{sorted(_CONSTANTS)}）")
    if isinstance(node, ast.BinOp):
        operation = _BIN_OPS.get(type(node.op))
        if operation is None:
            raise ValueError(f"不支持的运算符：{type(node.op).__name__}")
        return operation(_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp):
        operation = _UNARY_OPS.get(type(node.op))
        if operation is None:
            raise ValueError(f"不支持的一元运算符：{type(node.op).__name__}")
        return operation(_evaluate(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ValueError(f"不支持的函数（可用：{', '.join(_MATH_NAMES)}）")
        args = [_evaluate(arg) for arg in node.args]
        return _FUNCTIONS[node.func.id](*args)
    raise ValueError(f"不支持的表达式：{type(node).__name__}")


@tool("calc")
def calc(expression: str) -> dict[str, Any]:
    """计算一个数学表达式（四则运算、括号、幂、sqrt/log/sin/abs 等）。

    参数 expression：例如 "2 * (3 + 4)"、"sqrt(2)"、"pi * 2"。
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"表达式语法错误：{exc}") from exc
    value = _evaluate(tree)
    return {"expression": expression, "value": value}


@tool("math")
def math_fn(name: str, args: list[float] | None = None) -> dict[str, Any]:
    """调用一个高级数学函数。

    参数 name：函数名（sqrt / log / sin / factorial / gcd …）。
    参数 args：数字参数列表，例如 [16] 或 [2, 8]。
    """
    if name not in _FUNCTIONS:
        raise ValueError(f"未知函数 {name!r}（可用：{', '.join(_MATH_NAMES)}）")
    values = list(args or [])
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
    ):
        raise ValueError("args 里的每一项都必须是数字")
    value = _FUNCTIONS[name](*values)
    return {"name": name, "args": values, "value": value}


class ToolMathPlugin:
    name = "tool-math"
    inject = (TOOL_ADMIN_SERVICE,)

    def setup(self, ctx: Context):
        tools = ctx.require(TOOL_ADMIN_SERVICE)
        off_a = tools.register(calc.definition, calc.func, owner=self.name)
        off_b = tools.register(math_fn.definition, math_fn.func, owner=self.name)

        def dispose() -> None:
            off_b()
            off_a()

        return dispose


def tool_math_plugin(**config: Any) -> ToolMathPlugin:
    return ToolMathPlugin(**config)


__all__ = ["ToolMathPlugin", "calc", "math_fn", "tool_math_plugin"]
