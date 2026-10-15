"""安全计算器 —— calculate 工具的实现（AST 白名单求值，绝不用 eval）。

安全要点（P0 起沿用，未曾改变）：先 `ast.parse` 成语法树，再按白名单逐节点
求值；非白名单语法直接拒绝；指数有上限（防 `9**9**9` 这类指数炸弹）。
"""

from __future__ import annotations

import ast
import operator
from typing import Any

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


__all__ = ["calculate"]
