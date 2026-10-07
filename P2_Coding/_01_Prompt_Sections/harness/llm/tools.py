"""示例工具：安全计算器 + 极简工具箱。

工具箱是 `_03_Tool_Pipeline` 的前身：目前只有"注册 + 查表 + 错误包装"；
审批、超时、pre/post 管线都会在 _03 补上。

安全要点（从 P0 的 _03 沿用）：绝不用 eval() 执行模型生成的字符串。
先 ast.parse 成语法树，再按白名单逐节点求值；非白名单语法直接拒绝。
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.llm.vocabulary import ToolSpec

# ---------------------------------------------------------------------------
# 安全计算器
# ---------------------------------------------------------------------------

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_MAX_POW_EXPONENT = 100  # 防 9**9**9 这类指数炸弹拖垮进程


def _eval_node(node: ast.AST) -> float:
    """递归求值语法树节点；白名单之外的类型一律拒绝。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"不支持的常量：{node.value!r}")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BINARY_OPS:
            raise ValueError("不支持的运算符")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if op_type is ast.Pow and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError(f"指数超出上限 {_MAX_POW_EXPONENT}")
        return _BINARY_OPS[op_type](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"不支持的语法：{type(node).__name__}")


def calculate(expression: str) -> dict[str, Any]:
    """计算算术表达式，返回 {"expression": ..., "result": ...}。"""
    tree = ast.parse(expression, mode="eval")
    return {"expression": expression, "result": _eval_node(tree.body)}


CALCULATOR_SPEC = ToolSpec(
    name="calculate",
    description=(
        "计算一个算术表达式并返回精确结果。支持 + - * / // % ** 和括号。"
        "涉及任何算术时都必须用本工具，不要心算。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的算术表达式，例如 '1234*56.78'",
            }
        },
        "required": ["expression"],
    },
)


# ---------------------------------------------------------------------------
# 极简工具箱
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """一个工具 = 说明书 + 实现函数。"""

    spec: ToolSpec
    run: Callable[..., dict[str, Any]]


class Toolbox:
    """名字 → 工具 的注册表。

    执行契约：无论成功失败都返回字典；失败返回 {"error": "..."} ——
    错误是给模型的输入（铁律 #7），不是中断循环的理由。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, spec: ToolSpec, run: Callable[..., dict[str, Any]]) -> None:
        """注册一个工具。重复名字直接报错（fail loud，不静默覆盖）。"""
        if spec.name in self._tools:
            raise ValueError(f"工具重名：{spec.name}")
        self._tools[spec.name] = Tool(spec=spec, run=run)

    def specs(self) -> list[ToolSpec]:
        """全部工具说明书（装配进模型请求用）。"""
        return [tool.spec for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """按名字执行工具，失败转结构化错误。"""
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"未知工具：{name}"}
        try:
            return tool.run(**arguments)
        except Exception as exc:
            return {"error": f"工具执行失败：{exc}"}


def build_default_toolbox() -> Toolbox:
    """本练习使用的工具箱：一个计算器。"""
    toolbox = Toolbox()
    toolbox.register(CALCULATOR_SPEC, calculate)
    return toolbox
