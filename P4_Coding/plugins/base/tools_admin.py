"""tools-admin —— 工具面（基础插件）：注册表 + 执行管线 + 管理。

按用户的定义，**只有这一个服务**：`tool-admin`。

    ctx.require("tool-admin").register(definition, func, owner="tool-fs")
    ctx.require("tool-admin").execute("calc", {"expression": "1+1"})
    ctx.require("tool-admin").specs()      # 模型可见的 schema
    ctx.require("tool-admin").snapshot()   # 管理视图（按 owner 分组）

它自己不含任何业务工具——六个工具插件各自把工具登记进来。

**必要依赖**（插件级 `requires`）：`fs-sandbox` / `fs-observation-policy` /
`tool-math` / `tool-fs` / `tool-shell` / `tool-fs-search` / `tool-web-search` /
`tool-ask-user` / `proc-service`。缺一个，工具面就是残缺的——装配期 fail loud。

**四个事件插座**（工具插件的策略与观测都挂在这里）：

    tools/pre-execute    allow / deny / ask
    tools/execute        包住执行（超时 / 重试 / 记账）
    tools/post-execute   检查 / 替换结果
    tools/result         只读观测

**审批**：`needs_approval=True` 的工具会走审批通道；没有审批通道时
**fail-closed**（当作拒绝）。装配方可以在构造参数里给一个 policy。
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core import Context, Disposer

#: 工具服务的名字（**唯一**：注册、执行、管理都在这一个面上）。
TOOL_ADMIN_SERVICE = "tool-admin"


class _LazyApproval:
    """每次 `ask()` 现取 `approval` 服务；没有就 fail-closed。

    **为什么不能在 setup 时抓一份**：插件装载顺序无关（0042），审批通道可能
    排在工具面之后。抓一份的后果是"后来装上的审批永远不生效"——实测踩到：
    `--yes` 明明给了 allow，shell 仍然全部 denied。
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def ask(self, request: Any) -> bool:
        service = self._ctx.get("approval")
        if service is None or not hasattr(service, "ask"):
            return False  # fail-closed
        return bool(service.ask(request))


class ToolsAdminService:
    """工具面：注册表 + 管线 + 管理动作。

    工具插件面向它登记；agent-loop 面向它拿工具表与执行；管理面面向它
    做安装 / 卸载 / 停用 / 启用。三件事共用一个服务，因为它们共用同一份
    状态（注册表），拆开只会让消费方各拿一个门面。
    """

    def __init__(
        self,
        ctx: Context,
        *,
        approval: ApprovalPolicy | None = None,
        catalog: Mapping[str, Callable[..., object]] | None = None,
    ) -> None:
        self._ctx = ctx
        self._registry = ToolRegistry()
        # **用时现取**：approval 服务可能在本插件之后才装载（比如终端审批
        # 插件排在后面）。setup 时抓一份会让"后来装上的审批通道"永远不生效
        # ——P3 的 optional 依赖纪律：可选服务在用的那一刻取。
        self._pipeline = ToolPipeline(
            self._registry, hooks=ctx, approval=approval or _LazyApproval(ctx)
        )
        self._catalog = dict(catalog or {})

    # ------------------------------------------------------------------
    # 注册表
    # ------------------------------------------------------------------

    def register(
        self,
        definition: ToolDefinition,
        func: Callable[..., Any],
        *,
        owner: str,
    ) -> Disposer:
        """登记一件工具；返回注销句柄（幂等，认这一次注册）。"""
        return self._registry.register(definition, func, owner=owner)

    def unregister(self, name: str) -> None:
        self._registry.unregister(name)

    def get(self, name: str):
        return self._registry.get(name)

    def entries(self):
        return self._registry.entries()

    def owners(self):
        return self._registry.owners()

    def visible(self) -> list[ToolDefinition]:
        """模型可见的工具（只含启用的）。"""
        return self._registry.visible()

    def specs(self) -> list[dict[str, Any]]:
        """给模型看的 schema 列表。"""
        return self._registry.specs()

    def is_enabled(self, name: str) -> bool:
        return self._registry.is_enabled(name)

    def set_enabled(self, name: str, enabled: bool) -> None:
        self._registry.set_enabled(name, enabled)

    @property
    def disabled(self) -> list[str]:
        return self._registry.disabled

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def execute(
        self, name: str, arguments: Mapping[str, Any] | None = None, *, session_id: str | None = None
    ) -> ToolResult:
        return self._pipeline.execute(name, arguments or {}, session_id=session_id)

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """管理视图：按 owner 分组 + 计数 + 可安装列表。"""
        groups: list[dict[str, Any]] = []
        entries = self._registry.entries()
        for owner, names in self._registry.owners().items():
            items = []
            for name in names:
                record = entries[name]
                items.append(
                    {
                        "name": name,
                        "description": record.definition.description.splitlines()[0]
                        if record.definition.description
                        else "",
                        "enabled": record.enabled,
                        "needs_approval": record.definition.needs_approval,
                    }
                )
            groups.append({"owner": owner, "tools": items})
        return {
            "groups": groups,
            "counts": {
                "total": len(entries),
                "enabled": sum(1 for record in entries.values() if record.enabled),
                "disabled": len(self._registry.disabled),
            },
            "installable": [
                name for name in self._catalog if name not in self._ctx.active_plugins
            ],
        }

    def install(self, name: str) -> dict[str, Any]:
        """从白名单装载一个工具插件。"""
        factory = self._catalog.get(name)
        if factory is None:
            return {"ok": False, "message": f"{name!r} 不在可安装白名单里"}
        if name in self._ctx.active_plugins or name in self._ctx.pending_plugins:
            return {"ok": False, "message": f"{name!r} 已经装载了"}
        try:
            self._ctx.load_plugin(factory())
        except Exception as exc:  # noqa: BLE001 —— 管理动作回人话
            return {"ok": False, "message": f"安装 {name!r} 失败：{exc}"}
        return {"ok": True, "message": f"已安装 {name!r}"}

    def uninstall(self, owner: str) -> dict[str, Any]:
        """卸载提供这些工具的插件（工具跟着回卷）。"""
        if owner not in self._ctx.active_plugins:
            return {"ok": False, "message": f"插件不在装配里：{owner!r}"}
        try:
            self._ctx.unload_plugin(owner)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"卸载 {owner!r} 失败：{exc}"}
        return {"ok": True, "message": f"已卸载插件 {owner!r}"}

    def disable(self, name: str) -> dict[str, Any]:
        record = self._registry.get(name)
        if record is None:
            return {"ok": False, "message": f"工具不存在：{name!r}"}
        if not record.enabled:
            return {"ok": False, "message": f"{name!r} 已经是停用状态"}
        self._registry.set_enabled(name, False)
        self._ctx.emit("tools/disabled", tool=name, owner=record.owner)
        return {"ok": True, "message": f"已停用 {name!r}（模型立即看不到它）"}

    def enable(self, name: str) -> dict[str, Any]:
        record = self._registry.get(name)
        if record is None:
            return {"ok": False, "message": f"工具不存在：{name!r}"}
        if record.enabled:
            return {"ok": False, "message": f"{name!r} 已经是启用状态"}
        self._registry.set_enabled(name, True)
        self._ctx.emit("tools/enabled", tool=name, owner=record.owner)
        return {"ok": True, "message": f"已启用 {name!r}（下一轮请求生效）"}

    def act(self, name: str, action: str) -> dict[str, Any]:
        handler = {
            "install": self.install,
            "uninstall": self.uninstall,
            "disable": self.disable,
            "enable": self.enable,
        }.get(action)
        if handler is None:
            return {"ok": False, "message": f"未知动作：{action!r}"}
        return handler(name)


class ToolsAdminPlugin:
    """提供 `tool-admin` 服务。装配单里应声明 `role="base"`。"""

    name = "tools-admin"
    requires = (
        "fs-sandbox",
        "fs-observation-policy",
        "tool-math",
        "tool-fs",
        "tool-shell",
        "tool-fs-search",
        "tool-web-search",
        "tool-ask-user",
        "proc-service",

    )
    def __init__(
        self,
        *,
        approval: ApprovalPolicy | None = None,
        catalog: Mapping[str, Callable[..., object]] | None = None,
    ) -> None:
        self._approval = approval
        self._catalog = dict(catalog or {})

    def setup(self, ctx: Context) -> None:
        service = ToolsAdminService(ctx, approval=self._approval, catalog=self._catalog)
        ctx.provide(TOOL_ADMIN_SERVICE, service)

        # 两件自我管理工具（署名 tools）：看工具面 / 从白名单装一个插件。
        @tool("list_tools")
        def list_tools() -> dict[str, Any]:
            """列出当前工具面：名字、来自哪个插件、是否需要审批、是否停用。"""
            return {
                "tools": [
                    {
                        "name": name,
                        "owner": record.owner,
                        "needs_approval": record.definition.needs_approval,
                        "enabled": record.enabled,
                    }
                    for name, record in service.entries().items()
                ],
                "disabled": service.disabled,
            }

        @tool("install_tool", needs_approval=True)
        def install_tool(name: str) -> dict[str, Any]:
            """从白名单安装一个工具插件（需要审批）。参数 name：插件名。"""
            return service.install(name)

        service.register(list_tools.definition, list_tools.func, owner=self.name)
        service.register(install_tool.definition, install_tool.func, owner=self.name)


def tools_admin_plugin(**config: Any) -> ToolsAdminPlugin:
    """工厂：装配单的 `config` 可给 `approval` / `catalog`。"""
    return ToolsAdminPlugin(**config)


def _looks_like_result(value: Any) -> bool:
    """鸭子类型：一个工具结果只需要这四个属性（不 import 本模块）。"""
    return all(hasattr(value, name) for name in ("status", "content", "message"))


def _coerce_result(value: Any, started: float | None) -> ToolResult:
    """把"长得像结果"的对象转成本模块的 ToolResult（补时长）。"""
    if not _looks_like_result(value):
        return ToolResult(
            status="error",
            message=f"工具返回了无法识别的结果：{type(value).__name__}",
            error_code="TOOL_BAD_RESULT",
        )
    duration = getattr(value, "duration_ms", 0) or 0
    if started is not None:
        duration = int((time.monotonic() - started) * 1000)
    return ToolResult(
        status=str(getattr(value, "status", "error")),  # type: ignore[arg-type]
        content=getattr(value, "content", None),
        message=str(getattr(value, "message", "") or ""),
        duration_ms=duration,
        error_code=getattr(value, "error_code", None),


    )
__all__ = [
    "ApprovalPolicy",
    "AutoApprove",
    "AutoDeny",
    "DecoratedTool",
    "DuplicateToolError",
    "EXECUTE_EVENT",
    "POST_EVENT",
    "PRE_EVENT",
    "ParamSpec",
    "ParamType",
    "PreAction",
    "PreDecision",
    "RESULT_EVENT",
    "TOOL_ADMIN_SERVICE",
    "ToolDefinition",
    "ToolPipeline",
    "ToolRecord",
    "ToolRegistry",
    "ToolResult",
    "ToolStatus",
    "ToolsAdminPlugin",
    "ToolsAdminService",
    "UnknownToolError",
    "tool",
    "tools_admin_plugin",
    "validate_arguments",
]


# ======================================================================
# 以下合并自 components/tools/definition.py
# ======================================================================


#: 参数类型（对外词汇，与各家 function calling 的常见类型对齐）。
ParamType = Literal["string", "integer", "number", "boolean", "object", "array"]

#: 工具结果状态。
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
    """`@tool` 的返回值：定义 + 实现（注册时把两者一起交给注册表）。"""

    definition: ToolDefinition
    func: Callable[..., Any]


#: 注解 → 参数类型（`from __future__ import annotations` 下注解是字符串，
#: 可能是 `list[str]`、`float | None`、`dict[str, Any]` 这类形式）。
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


# ======================================================================
# 以下合并自 components/tools/registry.py
# ======================================================================


@dataclass
class ToolRecord:
    """注册表里的一条：定义 + 实现 + 署名 + 开关。"""

    definition: ToolDefinition
    func: Callable[..., Any]
    owner: str
    enabled: bool = True


class DuplicateToolError(ValueError):
    """同名工具重复注册（fail loud：不静默覆盖）。"""


class UnknownToolError(KeyError):
    """工具不存在（fail loud：防止拼写错误静默生效）。"""


class ToolRegistry:
    """工具面：注册 / 注销 / 查找 / 可见集合。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolRecord] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(
        self,
        definition: Any,
        func: Callable[..., Any],
        *,
        owner: str,
    ) -> Callable[[], None]:
        """登记一件工具；返回注销句柄（幂等，认这一次注册）。

        `definition` 可以是本模块的 `ToolDefinition`，也可以是**工具插件自己的
        副本**（只要有 name / description / parameters / needs_approval 四个
        属性）——插件之间不 import（决策 0053）。
        """
        if not isinstance(owner, str) or not owner:
            raise ValueError(f"工具 {definition.name!r} 缺少署名（owner）")
        if definition.name in self._tools:
            raise DuplicateToolError(
                f"工具已存在：{definition.name!r}"
                f"（来自 {self._tools[definition.name].owner!r}；不静默覆盖）"
            )
        record = ToolRecord(definition=definition, func=func, owner=owner)
        self._tools[definition.name] = record

        def unregister() -> None:
            if self._tools.get(definition.name) is record:
                self._tools.pop(definition.name, None)

        return unregister

    def unregister(self, name: str) -> None:
        record = self._tools.get(name)
        if record is None:
            raise UnknownToolError(f"工具不存在：{name!r}")
        self._tools.pop(name, None)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolRecord | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolRecord:
        record = self.get(name)
        if record is None:
            known = "、".join(self._tools) or "（注册表是空的）"
            raise UnknownToolError(f"工具不存在：{name!r}（当前：{known}）")
        return record

    def entries(self) -> dict[str, ToolRecord]:
        return dict(self._tools)

    def owners(self) -> dict[str, list[str]]:
        """owner → 工具名（按注册顺序）。"""
        groups: dict[str, list[str]] = {}
        for name, record in self._tools.items():
            groups.setdefault(record.owner, []).append(name)
        return groups

    def visible(self) -> list[ToolDefinition]:
        """模型可见的工具（按注册顺序）：只含启用的。"""
        return [record.definition for record in self._tools.values() if record.enabled]

    def specs(self) -> list[dict[str, Any]]:
        """给模型看的 schema 列表。"""
        return [definition.schema() for definition in self.visible()]

    def is_enabled(self, name: str) -> bool:
        return self.require(name).enabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.require(name).enabled = bool(enabled)

    @property
    def disabled(self) -> list[str]:
        return [name for name, record in self._tools.items() if not record.enabled]

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[str]:
        return iter(self._tools)


#: `@tool` 装饰器的转出（工具插件从 components 一层 import 就够）。
DecoratedTool = tool


# ======================================================================
# 以下合并自 components/tools/pipeline.py
# ======================================================================


#: pre-execute 的决策。
PreAction = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PreDecision:
    """审批策略的结论。"""

    action: PreAction = "allow"
    reason: str = ""


#: 事件名（使用方按这些名字订阅）。
PRE_EVENT = "tools/pre-execute"
EXECUTE_EVENT = "tools/execute"
POST_EVENT = "tools/post-execute"
RESULT_EVENT = "tools/result"

@dataclass(frozen=True)
class ToolApprovalRequest:
    """一次工具审批请求（给审批人看的全部信息）。

    与 `plugins.base.fs.ApprovalRequest` 同款形状：**一个 ask(request) 协议
    服务两边**——审批通道只实现一次，工具与文件共用。
    """

    tool: str
    arguments: Mapping[str, Any]
    session_id: str | None
    needs_approval: bool = True
    reason: str = ""


class ApprovalPolicy:
    """审批通道：`ask(request) -> bool`（True = 允许）。"""

    def ask(self, request: ToolApprovalRequest) -> bool:
        return False  # 默认 fail-closed


class AutoApprove:
    def ask(self, request: Any) -> bool:
        return True


class AutoDeny:
    def ask(self, request: Any) -> bool:
        return False


class ToolPipeline:
    """按固定顺序执行一次工具调用，并在四个点上发事件。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        hooks: Any,
        approval: ApprovalPolicy | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._registry = registry
        self._hooks = hooks
        self._approval = approval or AutoDeny()
        self._timeout = timeout

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        session_id: str | None = None,
    ) -> ToolResult:
        record = self._registry.get(name)
        if record is None or not record.enabled:
            return ToolResult(
                status="error",
                message=f"工具不可用：{name!r}（不存在或已停用）",
                error_code="TOOL_NOT_FOUND",
            )
        definition = record.definition

        # 1) 参数校验（模型写错参数 → 普通错误结果，不抛异常给上层）
        try:
            args = validate_arguments(definition, arguments)
        except ValueError as exc:
            return ToolResult(status="error", message=str(exc), error_code="TOOL_BAD_ARGUMENTS")

        # 2) pre-execute：策略可以 allow / deny / ask
        decision = self._hooks.waterfall(
            PRE_EVENT,
            PreDecision("ask" if definition.needs_approval else "allow"),
            tool=name,
            arguments=args,
            definition=definition,
            session_id=session_id,
        )
        if decision is None:
            decision = PreDecision("allow")
        if decision.action == "deny":
            return ToolResult(
                status="denied",
                message=decision.reason or f"工具 {name!r} 被策略拒绝",
                error_code="TOOL_DENIED",
            )
        if decision.action == "ask":
            request = ToolApprovalRequest(
                tool=name,
                arguments=args,
                session_id=session_id,
                needs_approval=True,
                reason=decision.reason,
            )
            if not self._approval.ask(request):
                return ToolResult(
                    status="denied",
                    message=decision.reason or f"工具 {name!r} 需要审批，但审批被拒绝",
                    error_code="TOOL_DENIED",

        # 3) execute：策略可以包住调用（超时 / 重试 / 记账）
                )
        def call() -> ToolResult:
            return self._invoke(record, args)

        wrapped = self._hooks.waterfall(
            EXECUTE_EVENT,
            call,
            tool=name,
            arguments=args,
            definition=definition,
            session_id=session_id,
        )
        if not callable(wrapped):
            return ToolResult(
                status="error",
                message="tools/execute 的 handler 必须返回可调用的执行函数",
                error_code="TOOL_PIPELINE_BROKEN",
            )
        started = time.monotonic()
        try:
            result = wrapped()
        except Exception as exc:  # noqa: BLE001 —— 工具抛错变成结果，不炸整个 turn
            result = ToolResult(
                status="error",
                message=f"{type(exc).__name__}: {exc}",
                error_code="TOOL_EXCEPTION",
        # 工具可以用本模块的 ToolResult，也可以用自己的副本——只要"长得像"
        # （status / content / message / error_code 四个属性）。插件之间不 import。
            )
        result = _coerce_result(result, started)

        # 4) post-execute：可以接受 / 替换 / 阻止
        final = self._hooks.waterfall(
            POST_EVENT,
            result,
            tool=name,
            arguments=args,
            definition=definition,
            session_id=session_id,
        )
        if not _looks_like_result(final):
            final = result

        # 5) result：只读观测
        self._hooks.emit(
            RESULT_EVENT,
            tool=name,
            result=final,
            arguments=args,
            session_id=session_id,
        )
        return final

    def _invoke(self, record: Any, args: dict[str, Any]) -> ToolResult:
        """在独立线程里执行（超时是**背压**：工具自己不守时也不会卡死 turn）。"""
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = record.func(**args)
            except BaseException as exc:  # noqa: BLE001 —— 收进结果
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=self._timeout)
        if thread.is_alive():
            return ToolResult(
                status="timeout",
                message=f"工具 {record.definition.name!r} 超过 {self._timeout}s 未返回",
                error_code="TOOL_TIMEOUT",
            )
        if "error" in box:
            exc = box["error"]
            return ToolResult(
                status="error",
                message=f"{type(exc).__name__}: {exc}",
                error_code=getattr(exc, "code", None) or "TOOL_EXCEPTION",
            )
        value = box.get("value")
        if _looks_like_result(value):
            return _coerce_result(value, None)
        return ToolResult(status="ok", content=value)
