"""变量插值 —— 把 section 里的 {{name}} 换成运行时上下文的值。

M4 第二步引入的最小词汇（_01 的 section 只能静态拼接）。语法故意做薄：

    {{name}}      变量名：字母/下划线开头，其余为字母/数字/下划线
    {{ name }}    两侧空白会被剥掉，等价写法

三条纪律（fail loud，铁律 #8 —— 宁可渲染失败，也不让模型看到半个 {{cwd}}）：
1. 引用了未提供的变量 → 报错（并列出本次上下文提供了哪些）；
2. 变量名非法（如 {{cwd-path}}）→ 报错；
3. 值必须是 str —— 运行时上下文负责把 Path/datetime 先格式化成字符串。

不做模板引擎的事：没有条件、循环、过滤器、转义。插值只回答一个问题——
"这一节提示词在**这一次**渲染里是什么文本"。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# 占位符：{{...}}，内部不允许再嵌花括号（避免误吞相邻的 }}）。
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def render_text(text: str, variables: Mapping[str, str]) -> str:
    """把 text 里的 {{name}} 全部替换为 variables[name]；未知/非法一律报错。"""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if not _VALID_NAME.match(name):
            raise ValueError("非法变量名：{{" + match.group(1) + "}}（应形如 {{name}}）")
        if name not in variables:
            known = "、".join(sorted(variables)) or "无"
            raise ValueError(f"未知变量：{name}（本次提供的变量：{known}）")
        value = variables[name]
        if not isinstance(value, str):
            raise ValueError(f"变量 {name} 的值必须是字符串，实际为 {type(value).__name__}")
        return value

    return _PLACEHOLDER.sub(substitute, text)


def referenced_names(text: str) -> list[str]:
    """按出现顺序列出 text 引用的变量名（追溯用；非法名原样列出，不做校验）。"""
    return [match.group(1).strip() for match in _PLACEHOLDER.finditer(text)]


__all__ = ["render_text", "referenced_names"]
