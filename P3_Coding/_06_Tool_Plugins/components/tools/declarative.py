"""声明式工具 —— 用**一个普通函数**定义一件工具（签名即参数表）。

**解决的问题**：写一件工具原本要三样东西凑齐才成——

    class Args(BaseModel):            # ① 参数模型
        path: str = Field(description="…")
    def read(path): ...               # ② 实现
    registry.register(define_tool("read_file", "读文件", schema_from_model(Args)), read)

三处名字/顺序都得对上，写一件小工具要摆三个地方。而对**模型自己写工具**这件事
（`_06` 的目标之一）来说更要命：模型吐出的是一段函数，让它同时写对 pydantic
模型、再写对 `define_tool` 的调用顺序，出错面太大。

**做法**：`@tool` 把一个普通函数**就地**变成工具——参数表与说明从签名和类型
注解里读，返回值就是工具结果。

    @tool("add", needs_approval=False)
    def add(a: int, b: int) -> dict:
        \"\"\"把两个整数相加。\"\"\"          # 文档串即工具说明（给模型看的）
        return {"sum": a + b}

    registry.register(add.definition, add, owner="tool-calc")

约定（都是"能一眼看出来"的规则）：

    · 参数名 + 类型注解 → JSON Schema 的属性与类型
    · 没有默认值 = 必填；有默认值 = 可选并带上默认值
    · 文档串第一段 → description（没有文档串就用 name 兜底，不报错）
    · 返回值必须是 dict（工具协议：错误也用 {"error": ...} 表达，不抛异常）

**为什么自己写这层而不直接用 pydantic 的 `create_model`**：这里的映射刻意
**只用得到的那一小块**（str / int / float / bool / list / dict + Optional），
不追求覆盖全部类型系统——映射越窄，规则越能一眼看懂，也越不容易在"注解写法
没料到"时静默产出坏 schema。遇到不认识的注解**当场报错**（fail loud），
不让模型写出一个 schema 与实现不一致的工具。

对应 dsh：`tools` 层里"一个函数即一件工具"的写法（本练习把它做成显式构件，
因为 `_06` 要允许工具**独立成插件**，声明越轻越好）。
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

from components.tools.registry import ToolDefinition, ToolFunc

#: 允许出现在参数注解里的标量类型 → JSON Schema 的 type。
_SCALARS: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}
#: 列表元素类型 → item 的 type（`list[int]` → {"type": "array", "items": {...}}）。
_ITEM_OF: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_for(annotation: Any, param: str, tool_name: str) -> dict[str, Any]:
    """一个参数注解 → JSON Schema 片段（不认识就 fail loud）。"""
    if annotation is inspect.Parameter.empty:
        raise TypeError(
            f"工具 {tool_name!r} 的参数 {param!r} 没有类型注解——"
            "参数表从注解派生，写清楚类型（如 `path: str`）"
        )
    if annotation in _SCALARS:
        return {"type": _SCALARS[annotation]}

    origin = get_origin(annotation)
    if origin in (list, tuple):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1 and args[0] in _ITEM_OF:
            return {"type": "array", "items": {"type": _ITEM_OF[args[0]]}}
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    if origin in (Union, types.UnionType):
        # Optional[X]（即 X | None）：按 X 的 schema 走（缺省即"可不传"）。
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _schema_for(args[0], param, tool_name)

    raise TypeError(
        f"工具 {tool_name!r} 的参数 {param!r} 用了不支持的注解 {annotation!r}——"
        "可用：str / int / float / bool / list / dict / list[这些] / 它们的 Optional"
    )


def schema_from_signature(func: Callable[..., Any], name: str) -> dict[str, Any]:
    """从函数签名派生 JSON Schema（工具 parameters 片段）。"""
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 —— 注解写坏了要说清是哪件工具
        hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for param_name, parameter in signature.parameters.items():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise TypeError(
                f"工具 {name!r} 用了 *args / **kwargs —— 工具的参数表要能被模型看懂，"
                "请把参数逐个列出来"
            )
        annotation = hints.get(param_name, parameter.annotation)
        piece = _schema_for(annotation, param_name, name)
        if parameter.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            piece["default"] = parameter.default
        properties[param_name] = piece

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def description_of(func: Callable[..., Any], name: str) -> str:
    """工具说明 = 文档串（取整段第一行块）；没有就用名字兜底。"""
    doc = inspect.getdoc(func) or ""
    doc = doc.strip()
    if not doc:
        return name
    return doc


@dataclass(frozen=True)
class DeclarativeTool:
    """`@tool` 的产物：一个**定义 + 实现**打包体（可直接交给 registry.register）。

    刻意不叫 ToolDefinition 的子类：这是"函数即工具"的那件东西，与"定义"是
    两回事——`definition` 是它派生出来的、真正进注册表的那一半。
    """

    definition: ToolDefinition
    func: ToolFunc

    @property
    def name(self) -> str:
        return self.definition.name

    def __call__(self, **arguments: Any) -> dict[str, Any]:
        """直接调用（测试与"手工跑一次"用；管线走的是 func）。"""
        return self.func(**arguments)


def tool(
    name: str,
    *,
    needs_approval: bool = False,
    description: str | None = None,
    review_path_arg: str = "",
) -> Callable[[Callable[..., Any]], DeclarativeTool]:
    """把函数变成工具（用法见模块说明）。

    - `needs_approval=True`：标记成危险工具，管线在 pre 阶段会先问审批；
    - `review_path_arg`：审批时把哪个参数（是路径）的文件摆给人看；
    - `description`：显式说明；不给则用文档串。
    """

    def decorate(func: Callable[..., Any]) -> DeclarativeTool:
        if not callable(func):
            raise TypeError(f"@tool 只接受函数：{func!r}")
        definition = ToolDefinition(
            name=name,
            description=description if description is not None else description_of(func, name),
            parameters=schema_from_signature(func, name),
            needs_approval=needs_approval,
            review_path_arg=review_path_arg,
        )
        return DeclarativeTool(definition=definition, func=func)

    return decorate


__all__ = [
    "DeclarativeTool",
    "description_of",
    "schema_from_signature",
    "tool",
]
