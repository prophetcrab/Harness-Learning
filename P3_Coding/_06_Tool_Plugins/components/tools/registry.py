"""工具注册表 —— 定义与实现分离，两层作用域 + 同名遮蔽 + **署名与开关**。

本练习相对 _01 的 `llm_seam.tools.Toolbox` 的两点升级：

1. **定义与实现分离**：`ToolDefinition`（声明：名字/说明/参数 schema/是否需审批）
   与实现函数（`Callable[..., dict]`）是两个东西，`define_tool()` 只产定义。
   同一份定义可以配不同实现（真实 / 假实现），这对测试极有价值。
2. **分层注册 + 遮蔽**：全局层之上可派生"作用域层"；解析时 most-specific-wins，
   同名的会话级工具遮蔽全局工具 —— 但全局定义不会因此消失，作用域丢弃后自动
   重新可见（对应铁律 #4"注册表分层 + 遮蔽"）。

P3 `_06` 新增的两样（都是为了让"工具面"**可被管理**）：

3. **owner（署名）**：每个登记项都要写清**是哪个插件登记的**。这不是装饰：
   工具管理面板按它分组、"卸载这个工具插件"要知道该卸谁、审计时要能回答
   "这件工具哪来的"。所以它**必填**——漏写当场报错（把约定变成不变量，
   与决策 0031 同一取向），而不是留个空字符串等着事后猜。
4. **enabled（开关）**：停用一件工具 = 它仍登记在册（定义与实现都在，随时能开
   回来），但**不可见也不可调用**：`visible()` / `resolve()` 都跳过停用的。
   于是对循环而言"停用"= 这件工具不存在，而不是"调用了才发现被拒"。

   为什么开关放注册表而不是"卸载再重装"：重装等于把插件整个重来一遍（别的
   副作用、装载顺序都跟着动），而"停用/启用"应当只影响**可见面**那一处。

对应 dsh：`packages/core/tools/src/index.ts` 的注册/解析部分；作用域遮蔽来自
`core/scope`（全局层 + 作用域覆盖层，most-specific-wins）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from components.llm.vocabulary import ToolSpec

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
    #: 审批时要展示哪个**文件**给审批人看（`_06`）：这里是参数名，不是路径。
    #:
    #: 为什么放在定义上：审批面板原先在**审批插件里写死了一张工具名清单**
    #: （只有 install_plugin 有预览），于是新加一个"安装类"工具（install_tool）
    #: 就悄悄变成**盲批**——审批人看不到自己要批准的那段代码。把"要看什么"
    #: 交给工具自己声明，少一处需要记得同步的清单。
    review_path_arg: str = ""

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
    review_path_arg: str = "",
) -> ToolDefinition:
    """声明一个工具（只产定义，不含实现）。实现稍后用 registry.register 绑定。

    `review_path_arg`：审批时把**哪个参数**（是路径）指向的文件摆给审批人看。
    """
    if not name:
        raise ValueError("工具名不能为空")
    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters
        if parameters is not None
        else {"type": "object", "properties": {}},
        needs_approval=needs_approval,
        review_path_arg=review_path_arg,
    )


@dataclass(frozen=True)
class RegisteredTool:
    """注册表里的一条记录：定义 + 实现 + 来源层 + **署名**（谁登记的）。

    `owner` 是插件名（`_06` 的工具管理面板按它分组）；作用域层登记项的 owner
    默认就是作用域名。
    """

    definition: ToolDefinition
    func: ToolFunc
    scope: str
    owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        """给界面/管理面看的形态（**不含实现**——那不可序列化）。"""
        return {
            "name": self.definition.name,
            "description": self.definition.description,
            "parameters": self.definition.parameters,
            "needs_approval": self.definition.needs_approval,
            "review_path_arg": self.definition.review_path_arg,
            "scope": self.scope,
            "owner": self.owner,
        }


class ToolRegistry:
    """全局工具注册表；可派生作用域视图（见 scoped）。"""

    def __init__(self) -> None:
        self._global: dict[str, RegisteredTool] = {}
        #: 被停用的工具名（登记还在，只是不可见/不可调用）。
        self._disabled: set[str] = set()

    # ---- 注册（全局层）----

    def register(self, definition: ToolDefinition, func: ToolFunc, *, owner: str) -> None:
        """注册到全局层。同名重复直接报错（fail loud，不静默覆盖）。

        `owner` 必填：它是"这件工具哪来的"的唯一答案（管理面板分组、卸载时该
        回卷谁、审计线索）。空署名视为写漏了——当场报错，不留空位。
        """
        if definition.name in self._global:
            raise ValueError(f"全局工具重名：{definition.name}")
        if not str(owner or "").strip():
            raise ValueError(
                f"登记工具 {definition.name!r} 必须署名（owner=插件名）："
                "工具管理面板按它分组，卸载时按它回卷"
            )
        self._global[definition.name] = RegisteredTool(
            definition, func, "global", str(owner)
        )

    def unregister(self, name: str) -> None:
        """注销全局层的一个工具；名字不存在直接报错（fail loud）。

        P3 新增的唯一处：插件卸载要能**回卷**自己注册的工具面
        （"注册即 effect"在工具注册表上的落地——工具箱插件的 disposer 调它）。
        """
        if name not in self._global:
            raise KeyError(f"全局工具不存在：{name}")
        del self._global[name]
        self._disabled.discard(name)   # 停用状态跟着登记一起消失，不留孤儿

    # ---- 开关（停用 / 启用）----

    def set_enabled(self, name: str, enabled: bool) -> None:
        """停用/启用一件已登记的工具（登记本身不动）。"""
        if name not in self._global:
            raise KeyError(f"全局工具不存在：{name}")
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def is_enabled(self, name: str) -> bool:
        """这件工具当前可见吗（未登记也算不可见）。"""
        return name in self._global and name not in self._disabled

    @property
    def disabled(self) -> list[str]:
        """当前被停用的工具名（按登记顺序）。"""
        return [name for name in self._global if name in self._disabled]

    # ---- 派生作用域 ----

    def scoped(self, name: str) -> ToolScope:
        """派生一个作用域视图：读穿透到全局，写/遮蔽只影响本作用域。"""
        return ToolScope(self, name)

    # ---- 解析 ----

    def resolve(self, tool_name: str) -> RegisteredTool | None:
        """取一件**可用**的工具：停用的等同于不存在（模型看不到也调不到）。"""
        if tool_name in self._disabled:
            return None
        return self._global.get(tool_name)

    def registered(self, tool_name: str) -> RegisteredTool | None:
        """取一件**登记在册**的工具（含已停用的）——管理面用。"""
        return self._global.get(tool_name)

    def entries(self) -> dict[str, RegisteredTool]:
        """全部登记项（含停用的），登记顺序。管理面板与审计看这个。"""
        return dict(self._global)

    def owners(self) -> dict[str, list[str]]:
        """owner → 它登记的工具名（登记顺序）。工具管理面板按这个分组。"""
        grouped: dict[str, list[str]] = {}
        for name, tool in self._global.items():
            grouped.setdefault(tool.owner, []).append(name)
        return grouped

    def visible(self) -> dict[str, RegisteredTool]:
        """当前可见的 (名字 → 工具) 映射（停用的不出现）。"""
        return {
            name: tool
            for name, tool in self._global.items()
            if name not in self._disabled
        }

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

    def register(self, definition: ToolDefinition, func: ToolFunc, owner: str = "") -> None:
        """注册/覆盖到本作用域层（同名覆盖本层旧定义是允许的）。

        作用域层的署名默认就是作用域名——本层工具从来就是"临时给这个作用域用
        的"，owner 写缺省值也不会让管理面失明。
        """
        self._local[definition.name] = RegisteredTool(
            definition, func, self._name, owner or self._name
        )

    def resolve(self, tool_name: str) -> RegisteredTool | None:
        """先查本层，再穿透到全局 —— 本层定义优先（全局的停用状态照样生效）。"""
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
