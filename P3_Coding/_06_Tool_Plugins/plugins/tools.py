"""tools 插件 —— **工具面本身**（注册表 + 管理中心）。

**`_06` 的拆分**：这个插件原先（`_01`–`_05`）把八件工具**焊在里面**：计算、
读写、检索、命令全在一个 `ToolsPlugin.setup` 里注册。于是"想少给模型几件工具"
只能改代码，"想加一件工具"也得改同一个文件——工具面既是**底座**又是**内容**，
两件事被绑死了。

现在分成两层：

    tools 插件（本模块）        底座：提供**空的** `tools` 服务（ToolRegistry）
                               + `tool-admin`（台账与开关）+ 两件自我管理工具
    tool-* 插件（一件/一类工具）内容：各自 inject tools，往这个空注册表里登记

好处正是用户提的三条诉求：

1. **解耦**：工具插件之间**互不依赖**——只依赖 `tools` 服务（同一个注册表）
   与各自需要的能力插件（fs / subprocess）。装卸任何一个都不牵动别人。
2. **可管理**：`tool-admin` 提供"谁登记了哪些工具 + 一键停用/启用/卸载"，
   界面在管理弹层里多一个「工具」标签页（自述见 `_admin_spec`）。
3. **可自扩展**：`install_tool` 让模型**从工作区的 .py 装一个新工具插件**
   （先人审源码、再装载——与 `install_plugin` 同一条安全边界）。

**为什么"卸载工具"落在插件上**：工具来自插件；要一劳永逸地去掉它，就得把署名
的那个插件一起撤掉。所以面板里每件工具都显示 owner，"卸载"作用在 owner 上。

**为什么还要 tool-admin，不复用 plugin-admin**：plugin-admin 管的是"应用的插件
台账"（分类、base 不可卸），是**装配级**策略；工具管理管的是"眼前这个工具面"
（owner / 审批标记 / 开关），且没有 tools 插件就根本不存在。两者策略不同、
生命周期不同，合并会让"关掉一件工具"牵扯到应用台账。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from components.shell import AdminSpec, RouteRequest, RouteSpec, Surface
from components.tools import ApprovalPolicy, ToolRegistry, tool
from components.tools import gate as _gate
from kernel import Context
from plugins.services import (
    APPROVAL_SERVICE,
    FS_SERVICE,
    TOOLS_ADMIN_SERVICE,
    TOOLS_SERVICE,
)

#: 动作的中文文案（快照下发，界面照抄——壳不认识"停用/卸载"是什么）。
TOOL_ACTIONS = {
    "disable": "停用",
    "enable": "启用",
    "unload": "卸载插件",
}

#: 状态的中文（与插件台账的用词保持一套）。
TOOL_STATE = {
    "enabled": "可用",
    "disabled": "已停用",
}


def _first_line(text: str) -> str:
    """说明的第一行（卡片上只放一行；全文留给 title）。"""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0]
    return line[:88] + "…" if len(line) > 88 else line


def _owner_hint(ctx: Context, owner: str) -> str:
    """分组下的一句话：这些工具来自哪个插件、它当前在不在。"""
    info = next((i for i in ctx.plugins if i.name == owner), None)
    if info is None:
        return "提供这些工具的插件已不在台账（工具仍登记着）"
    state = {
        "active": "运行中", "pending": "等待依赖",
        "disabled": "已停用", "failed": "激活失败",
    }.get(info.status, info.status)
    return f"来自插件 {owner}（{state}）"


class ToolsAdminService:
    """工具面管理：台账（按 owner 分组）+ 停用/启用 + 卸载 owner + 装新工具插件。

    所有动作返回 `{"ok": bool, "message": str}`——**失败也给人话**，不抛异常给
    界面（与 plugin-admin 同一原则：人点错了要看到原因，不是 traceback）。
    """

    def __init__(
        self,
        ctx: Context,
        registry: ToolRegistry,
        *,
        policy: Callable[[], ApprovalPolicy | None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._registry = registry
        #: 取审批策略（装配方直接给的那份，或 optional 服务里的）。
        #: **现取而不是抓存**：approval 插件可能在本插件之后才装上（顺序无关）。
        self._policy = policy if policy is not None else (lambda: ctx.get(APPROVAL_SERVICE))

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """给界面的视图：按 owner 分组的工具面（形状沿用插件快照）。

        字段名与 `_05` 的插件快照一致（groups/plugins/actions）——**一份渲染器
        同时服务插件页与工具页**；字段分叉就会静默少画一块。
        """
        groups: list[dict[str, Any]] = []
        for owner, names in self._registry.owners().items():
            entries: list[dict[str, Any]] = []
            for name in names:
                record = self._registry.registered(name)
                if record is None:      # 并发卸载：跳过而不是崩
                    continue
                enabled = self._registry.is_enabled(name)
                tags: list[dict[str, str]] = []
                if record.definition.needs_approval:
                    tags.append({
                        "text": "需审批", "kind": "warn",
                        "title": "调用前需要人工审批",
                    })
                if not enabled:
                    tags.append({
                        "text": "已停用", "kind": "off",
                        "title": "模型看不到它，也调不到",
                    })
                can_unload = owner in self._ctx.active_plugins
                entries.append({
                    "name": name,
                    "status": "enabled" if enabled else "disabled",
                    "category": owner,
                    "tags": tags,
                    "note": _first_line(record.definition.description),
                    "actions": (["disable"] if enabled else ["enable"]) + (["unload"] if can_unload else []),
                    "blocked_reason": "" if can_unload else "提供它的插件已不在装配里",
                })
            groups.append({
                "category": owner,
                "label": owner,
                "hint": _owner_hint(self._ctx, owner),
                "plugins": entries,
            })

        return {
            "groups": groups,
            "installable": self._installable(),
            "actions": dict(TOOL_ACTIONS),
            "counts": {
                "total": len(self._registry.entries()),
                "active": len(self._registry.visible()),
                "disabled": len(self._registry.disabled),
            },
        }

    def _installable(self) -> list[dict[str, Any]]:
        """还能装的工具插件（白名单里、当前没装）。"""
        from plugins.catalog import TOOL_PLUGIN_NAMES, default_catalog

        catalog = default_catalog()
        loaded = set(self._ctx.active_plugins) | set(self._ctx.pending_plugins)
        return [
            {"name": name, "actions": ["install"]}
            for name in TOOL_PLUGIN_NAMES
            if name in catalog and name not in loaded
        ]

    # ------------------------------------------------------------------
    # 写：开关与装卸
    # ------------------------------------------------------------------

    def disable(self, name: str) -> dict[str, Any]:
        """停用一件工具：登记留着，从工具面（模型可见集）里撤掉。"""
        record = self._registry.registered(name)
        if record is None:
            return {"ok": False, "message": f"工具不存在：{name}"}
        if not self._registry.is_enabled(name):
            return {"ok": False, "message": f"{name} 已经是停用状态"}
        self._registry.set_enabled(name, False)
        self._ctx.emit("tools/disabled", tool=name, owner=record.owner)
        return {"ok": True, "message": f"已停用 {name}（模型立即看不到它）"}

    def enable(self, name: str) -> dict[str, Any]:
        """启用一件工具：重新出现在工具面里（下一轮请求生效）。"""
        record = self._registry.registered(name)
        if record is None:
            return {"ok": False, "message": f"工具不存在：{name}"}
        if self._registry.is_enabled(name):
            return {"ok": False, "message": f"{name} 已经是启用状态"}
        self._registry.set_enabled(name, True)
        self._ctx.emit("tools/enabled", tool=name, owner=record.owner)
        return {"ok": True, "message": f"已启用 {name}（下一轮对话生效）"}

    def unload_owning_plugin(self, name: str) -> dict[str, Any]:
        """卸载某件工具**所属的插件**（工具跟着一起回卷）。

        `name` 给插件名（分组卡上的"卸载"）或它登记的任一工具名都可以——
        后者更精确地表达"我不想要这件工具"，落到同一个动作上。
        """
        record = self._registry.registered(name)
        owner = record.owner if record is not None else name
        if owner not in self._ctx.active_plugins:
            return {"ok": False, "message": f"提供这些工具的插件不在装配里：{owner}"}
        tools = [
            tool_name
            for tool_name, entry in self._registry.entries().items()
            if entry.owner == owner
        ]
        try:
            self._ctx.unload_plugin(owner)
        except Exception as exc:  # noqa: BLE001 —— 卸载失败也是给人看的结果
            return {"ok": False, "message": f"卸载 {owner} 失败：{exc}"}
        tail = f"（连带 {len(tools)} 件工具：{'、'.join(tools)}）" if tools else ""
        return {"ok": True, "message": f"已卸载插件 {owner}{tail}"}

    def install(self, name: str) -> dict[str, Any]:
        """从白名单装载一个工具插件（人点的按钮不需要审批——见 plugin-admin 的说明）。"""
        from plugins.catalog import default_catalog

        factory = default_catalog().get(name)
        if factory is None:
            return {"ok": False, "message": f"{name} 不在可安装白名单里"}
        if name in set(self._ctx.active_plugins) | set(self._ctx.pending_plugins):
            return {"ok": False, "message": f"{name} 已经装载了"}
        try:
            self._ctx.load_plugin(factory())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"安装 {name} 失败：{exc}"}
        return {"ok": True, "message": f"已安装 {name}"}

    def act(self, name: str, action: str) -> dict[str, Any]:
        """按名字分派动作（路由与界面共用这一处）。"""
        handler = {
            "disable": self.disable,
            "enable": self.enable,
            "unload": self.unload_owning_plugin,
            "install": self.install,
        }.get(action)
        if handler is None:
            return {"ok": False, "message": f"未知动作：{action}"}
        return handler(name)

    # ------------------------------------------------------------------
    # 模型路径：装一个新工具插件（先审批，再装载）
    # ------------------------------------------------------------------

    def install_plugin_from_source(self, path: str, entry: str | None = None) -> dict[str, Any]:
        """把工作区里的一个 .py 装成**工具插件**：读源码 → 审批 → 装载。

        与 `self_tools.install` 的区别只有一处：这里装出来的插件**以本注册表为
        目标**，所以回报里会带上"它登记了哪些工具"（模型据此知道自己刚多了什么
        本事）。安全边界完全相同：人工审批 + 只读工作区 + 只收 .py。

        审批的去重靠 `components/tools/gate.py`：工具路径下管线已经问过一次
        （`install_tool` 标了 needs_approval），服务层不再弹第二张卡；直接调用
        服务（测试 / 别的入口）则由服务自己问，问不到即拒绝。
        """
        if not path.endswith(".py"):
            return {"error": f"只接受 .py 文件：{path}", "code": "INSTALL_BAD_PATH"}

        fs = self._ctx.get(FS_SERVICE)
        if fs is None:
            return {"error": "没有文件系统服务，无法读取插件源码", "code": "INSTALL_NO_FS"}
        try:
            source = fs.read_text(path)
        except Exception as exc:  # noqa: BLE001 —— 读不到就是给模型的输入
            return {"error": f"读取插件源码失败：{exc}", "code": "INSTALL_READ_FAILED"}

        if not _gate.is_preapproved("install_tool"):
            decision = _gate.source_approval(
                self._policy(),
                tool_name="install_tool",
                path=path,
                source=source,
                reason="安装新工具插件（会引入可执行代码）——请确认源码",
            )
            if not decision.allowed:
                return {
                    "error": f"安装被拒绝：{decision.reason}",
                    "code": "INSTALL_DENIED",
                    "path": path,
                }

        from kernel.loader import plugin_from_source

        try:
            plugin, _factory, name = plugin_from_source(source, path=path, entry=entry)
        except Exception as exc:  # noqa: BLE001 —— 代码有问题，如实回给模型
            return {"error": f"解析插件失败：{exc}", "code": "INSTALL_PARSE_FAILED"}

        before = set(self._registry.entries())
        try:
            self._ctx.load_plugin(plugin)
        except Exception as exc:  # noqa: BLE001 —— 内核保证不留半装状态
            return {"error": f"装载失败：{exc}", "code": "INSTALL_LOAD_FAILED"}

        added = sorted(set(self._registry.entries()) - before)
        result: dict[str, Any] = {
            "installed": name,
            "source": path,
            "tools_added": added,
        }
        info = next((i for i in self._ctx.plugins if i.name == name), None)
        status = info.status if info is not None else "unknown"
        if not added:
            result["note"] = "插件装上了，但它没有登记任何工具（这确定是工具插件吗？）"
        elif status != "active":
            missing = list(info.missing) if info is not None else []
            result["status"] = status
            result["note"] = f"插件没激活（缺依赖 {missing}），工具暂不可用"
        else:
            result["note"] = "新工具从**下一轮**对话开始出现在工具面里"
        return result


# ---------------------------------------------------------------------------
# 模型可见的两件工具：看工具面 / 装新工具插件
# ---------------------------------------------------------------------------


def _register_self_tools(registry: ToolRegistry, service: ToolsAdminService) -> tuple[str, ...]:
    """登记"模型能管理工具面"的两件工具（署名 owner="tools"：它们是底座的自我管理）。"""

    @tool("list_tools")
    def list_tools() -> dict[str, Any]:
        """列出当前工具面：每件工具的名字、来自哪个插件、是否需要审批、是否被停用。

        装新工具前先看这里，避免重名；也可以用它确认自己刚装的工具生效了没有。
        """
        return {
            "tools": [
                {
                    "name": name,
                    "owner": record.owner,
                    "needs_approval": record.definition.needs_approval,
                    "enabled": registry.is_enabled(name),
                }
                for name, record in registry.entries().items()
            ],
            "disabled": registry.disabled,
        }

    @tool("install_tool", needs_approval=True, review_path_arg="path")
    def install_tool(path: str, entry: str | None = None) -> dict[str, Any]:
        """从工作区里的 .py 文件安装一个新**工具插件**（这条路径允许引入新代码）。

        需要人工审批——审批人会看到源码全文，批准后才装载；被拒则什么都不发生。

        参数 path：工作区内的 .py 文件路径（相对工作区根），例如 'plugins/my_tool.py'。
        参数 entry：工厂函数名（可选）；不给则自动找 factory / <文件名>_plugin /
        唯一一个带 setup 方法的类。

        工具插件的写法（把一个工具登记进 tools 服务；`@tool` 让签名就是参数表）：

            from components.tools import tool
            from plugins.services import TOOLS_SERVICE

            @tool("greet")
            def greet(name: str) -> dict:
                \"\"\"向某人问好。\"\"\"
                return {"hello": name}

            class GreetPlugin:
                name = "tool-greet"
                inject = (TOOLS_SERVICE,)

                def setup(self, ctx):
                    registry = ctx.require(TOOLS_SERVICE)
                    registry.register(greet.definition, greet.func, owner=self.name)

                    def dispose():
                        registry.unregister("greet")
                    return dispose
        """
        with _gate.preapproved("install_tool"):
            return service.install_plugin_from_source(path, entry)

    registry.register(list_tools.definition, list_tools.func, owner="tools")
    registry.register(install_tool.definition, install_tool.func, owner="tools")
    return ("list_tools", "install_tool")


# ---------------------------------------------------------------------------
# 管理页自述（管理弹层里的「工具」标签页）+ 它的两个端点
# ---------------------------------------------------------------------------


def _admin_spec() -> AdminSpec:
    return AdminSpec(
        key="tools",
        label="工具",
        title="工具管理",
        summary="每件工具都带署名（哪个插件给的）：停用只影响工具面，卸载会把插件一起撤掉。",
        snapshot="api/tools",
        action="api/tools/{name}/{action}",
        foot="<b>停用</b>只把工具从模型眼前撤掉（登记还在，随时能开回来）；"
             "<b>卸载插件</b>会把提供它的插件一起撤掉，那些工具就彻底没了。"
             "人点按钮不需要审批——审批拦的是模型发起的那条路径。",
        order=20,                    # 插件台账（order=10）之后
    )


def _admin_routes():
    """本插件的两个端点。handler 完全不碰 HTTP——分块/分帧/收尾都是壳的事。"""

    def list_tools(request: RouteRequest) -> None:
        admin = request.ctx.get(TOOLS_ADMIN_SERVICE)
        if admin is None:
            request.json({"error": "工具管理服务未就绪"}, status=503)
            return
        request.json(admin.snapshot())

    def act(request: RouteRequest) -> None:
        admin = request.ctx.get(TOOLS_ADMIN_SERVICE)
        if admin is None:
            request.json({"error": "工具管理服务未就绪"}, status=503)
            return
        result = admin.act(request.params["name"], request.params["action"])
        request.json({**result, "snapshot": admin.snapshot()})

    return (
        RouteSpec("GET", "/api/tools", list_tools, order=5),
        RouteSpec("POST", "/api/tools/<name>/<action>", act, order=6),
    )


class ToolsPlugin:
    """工具面底座：**空注册表** + 工具管理 + 管理页自述。

    它自己不再登记任何业务工具——那由 tool-* 插件各自完成（`_06` 的拆分）。
    它保证两件事："有个注册表"（别的插件往上登记），"这个注册表看得见、管得着"。
    """

    name = "tools"
    #: 应用角色（base）：工具面本身。没有它，模型没有手，别的工具插件也无处登记。
    category = "base"
    #: 可选：审批策略（装新工具插件要问；缺失 → fail-closed 拒绝）与文件系统
    #: （装新工具插件要读源码；缺失 → 如实报错，工具面本身照常可用）。
    optional = (APPROVAL_SERVICE, FS_SERVICE)

    def __init__(self, *, approval: ApprovalPolicy | None = None) -> None:
        #: 直接给策略的场合（测试 / 特殊装配）；正常装配用 optional 里的 approval 服务。
        #: 装配方后来才装上 approval 插件时（顺序无关），_policy() 会现取到它。
        self._approval = approval

    def setup(self, ctx: Context):
        registry = ToolRegistry()
        service = ToolsAdminService(ctx, registry, policy=self._policy_of(ctx))
        names = _register_self_tools(registry, service)

        ctx.provide(TOOLS_SERVICE, registry)
        ctx.provide(TOOLS_ADMIN_SERVICE, service)
        ctx.provide(
            f"{TOOLS_SERVICE}.surface",
            Surface(admins=(_admin_spec(),), routes=_admin_routes()),
        )

        def dispose() -> None:
            for name in names:
                registry.unregister(name)

        return dispose

    def _policy_of(self, ctx: Context) -> Callable[[], ApprovalPolicy | None]:
        """装配方直接给了策略就用它，否则现取 optional 的 approval 服务。"""
        if self._approval is not None:
            return lambda: self._approval
        return lambda: ctx.get(APPROVAL_SERVICE)


def tools_plugin(*, approval: ApprovalPolicy | None = None) -> ToolsPlugin:
    """工厂：`approval` 给了就直接用它问审批（不给则用 optional 的 approval 服务）。"""
    return ToolsPlugin(approval=approval)


__all__ = [
    "TOOL_ACTIONS",
    "TOOL_STATE",
    "ToolsAdminService",
    "ToolsPlugin",
    "tools_plugin",
]
