"""工具注册表 —— 定义与实现分离，两层作用域 + 同名遮蔽。

本练习相对 _01 的 `llm_seam.tools.Toolbox` 的两点升级：

1. **定义与实现分离**：`ToolDefinition`（声明：名字/说明/参数 schema/是否需审批）
   与实现函数（`Callable[..., dict]`）是两个东西，`define_tool()` 只产定义。
   同一份定义可以配不同实现（真实 / 假实现），这对测试极有价值。
2. **分层注册 + 遮蔽**：全局层之上可派生"作用域层"；解析时 most-specific-wins，
   同名的会话级工具遮蔽全局工具 —— 但全局定义不会因此消失，作用域丢弃后自动
   重新可见（对应铁律 #4"注册表分层 + 遮蔽"）。

对应 dsh：`packages/core/tools/src/index.ts` 的注册/解析部分；作用域遮蔽来自
`core/scope`（全局层 + 作用域覆盖层，most-specific-wins）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.llm.vocabulary import ToolSpec

# 工具实现：形如 func(**arguments) -> dict，错误以 {"error": ...} 返回（不抛异常）。
ToolFunc = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    """一个工具的"定义"（声明式），不携带实现。

    needs_approval 是给管线看的危险标记：为 True 时，管线在 pre 阶段会
    转入"ask"（咨询审批策略），除非有中间件已经显式 allow/deny。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False

    def spec(self) -> ToolSpec:
        """转成给模型看的说明书（装配进模型请求）。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


def define_tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    *,
    needs_approval: bool = False,
) -> ToolDefinition:
    """声明一个工具（只产定义，不含实现）。实现稍后用 registry.register 绑定。"""
    if not name:
        raise ValueError("工具名不能为空")
    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters
        if parameters is not None
        else {"type": "object", "properties": {}},
        needs_approval=needs_approval,
    )


@dataclass(frozen=True)
class RegisteredTool:
    """注册表里的一条记录：定义 + 实现 + 来源层（用于调试与遮蔽审计）。"""

    definition: ToolDefinition
    func: ToolFunc
    scope: str


class ToolRegistry:
    """全局工具注册表；可派生作用域视图（见 scoped）。"""

    def __init__(self) -> None:
        self._global: dict[str, RegisteredTool] = {}

    # ---- 注册（全局层）----

    def register(self, definition: ToolDefinition, func: ToolFunc) -> None:
        """注册到全局层。同名重复直接报错（fail loud，不静默覆盖）。"""
        if definition.name in self._global:
            raise ValueError(f"全局工具重名：{definition.name}")
        self._global[definition.name] = RegisteredTool(definition, func, "global")

    # ---- 派生作用域 ----

    def scoped(self, name: str) -> ToolScope:
        """派生一个作用域视图：读穿透到全局，写/遮蔽只影响本作用域。"""
        return ToolScope(self, name)

    # ---- 解析 ----

    def resolve(self, tool_name: str) -> RegisteredTool | None:
        return self._global.get(tool_name)

    def visible(self) -> dict[str, RegisteredTool]:
        """当前可见的 (名字 → 工具) 映射（全局层把所有工具都视为可见）。"""
        return dict(self._global)

    def specs(self) -> list[ToolSpec]:
        """当前可见工具的全部说明书（装配进模型请求）。"""
        return [tool.definition.spec() for tool in self.visible().values()]

    @property
    def names(self) -> list[str]:
        return list(self.visible())


class ToolScope:
    """一个作用域覆盖层：同名遮蔽全局（most-specific-wins）。

    遮蔽发生在"解析时"，不是注册时删除：全局的同名工具仍留在全局层，
    只是被本层的定义挡住；本层被丢弃后，全局定义自动重新可见。
    """

    def __init__(self, parent: ToolRegistry, name: str) -> None:
        self._parent = parent
        self._name = name
        self._local: dict[str, RegisteredTool] = {}

    @property
    def scope_name(self) -> str:
        return self._name

    def register(self, definition: ToolDefinition, func: ToolFunc) -> None:
        """注册/覆盖到本作用域层（同名覆盖本层旧定义是允许的）。"""
        self._local[definition.name] = RegisteredTool(definition, func, self._name)

    def resolve(self, tool_name: str) -> RegisteredTool | None:
        """先查本层，再穿透到全局 —— 本层定义优先。"""
        return self._local.get(tool_name) or self._parent.resolve(tool_name)

    def visible(self) -> dict[str, RegisteredTool]:
        """合并视图：全局可见集 + 本层覆盖。"""
        merged = self._parent.visible()
        merged.update(self._local)  # 同名覆盖，未同名则新增
        return merged

    def specs(self) -> list[ToolSpec]:
        return [tool.definition.spec() for tool in self.visible().values()]

    @property
    def names(self) -> list[str]:
        return list(self.visible())
