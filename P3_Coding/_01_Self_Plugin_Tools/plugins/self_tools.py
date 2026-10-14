"""self-tools 插件 —— 自扩展工具：让模型装卸自己所在进程的插件（P3 `_01` 的唯一机制）。

三件工具（外加一个同名服务，工具只是服务的薄包装）：

    list_plugins    只读：插件台账（名字/状态/依赖/能否卸载）+ 可装载/可卸载清单
                    + 当前工具面。无需审批。
    load_plugin     装载**可装载白名单**（catalog）里的插件；它提供的服务与工具
                    **从下一轮对话开始**可用。需要人工审批。
    unload_plugin   卸载**可卸载白名单**（unloadable）里的插件；依赖它的插件
                    自动退位。需要人工审批。

**两条安全边界**（白名单限定 + 审批，双保险）：

1. **可装载 = catalog 里有名字**：catalog 是"插件名 → 工厂函数"的显式映射，
   模型只能装载预先批准过的插件——它给的是一个**名字**，不是代码；本实现没有
   "从磁盘发现插件"这回事，所以模型无法凭空引入新代码。
2. **可卸载 = unloadable 里有名字**：默认只有示例插件 `clock`。卸载一个还在被
   使用的能力会破坏当前会话（最典型的是卸载 webui——它正是网页对话的传输通道，
   一卸连接就断）；要放开哪些名字，由装配方显式决定。

**审批**：`load_plugin` / `unload_plugin` 都标了 `needs_approval`——与写文件、
跑命令同级。审批策略没配、或人拒绝时 **fail-closed**：什么都不会发生，工具返回
结构化拒绝（错误是给模型的输入，循环不受影响）。

**可观测**：每次装卸发 `plugin/loaded` / `plugin/unloaded` 内核事件（供订阅者）；
工具调用本身会进会话日志（tool/result 事件）——"模型动过什么"有据可查。

**线程说明**：工具在管线的**工作线程**里执行（工具管线带超时）；同一时刻只有
一个 turn 在跑（agent 的锁），因此对内核的访问仍是串行的。装卸是同步的：装载
失败会连同已登记的副作用一起回卷，不留半装状态。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from components.tools import define_tool, schema_from_model
from kernel import Context, plugin_name
from plugins.events import PLUGIN_LOADED, PLUGIN_UNLOADED
from plugins.services import SELF_TOOLS_SERVICE, TOOLS_SERVICE

#: 默认的可装载白名单：插件名 → 工厂函数（惰性导入，避免模块级循环依赖）。
DEFAULT_CATALOG_NAMES = ("clock", "webui", "rpc")
#: 默认可卸载白名单：只放"卸载也不会破坏当前会话"的插件。
DEFAULT_UNLOADABLE = ("clock",)


def default_catalog() -> dict[str, Callable[[], object]]:
    """默认白名单的三个可选插件（都是"装了有用、卸了不伤人"的类型）。"""
    from plugins.clock import clock_plugin
    from plugins.rpc import rpc_plugin
    from plugins.webui import webui_plugin

    return {
        "clock": clock_plugin,
        # 界面：端口交给系统分配（port=0），装配即起、卸载即停
        "webui": lambda: webui_plugin(port=0),
        # RPC：TCP 监听同样交给系统分配（listen=0）
        "rpc": lambda: rpc_plugin(listen=0),
    }


class LoadPluginArgs(BaseModel):
    name: str = Field(
        description="要装载的插件名（取值见 list_plugins 返回的 loadable 清单）"
    )


class UnloadPluginArgs(BaseModel):
    name: str = Field(
        description="要卸载的插件名（取值见 list_plugins 返回的 unloadable 清单）"
    )


def _endpoint_of(service: Any) -> str | None:
    """服务的"地址"（有就告诉模型）：webui 有 .url，rpc 有 .address。"""
    url = getattr(service, "url", None)
    if isinstance(url, str):
        return url
    address = getattr(service, "address", None)
    if isinstance(address, tuple) and len(address) == 2:
        return f"{address[0]}:{address[1]}"
    return None


class SelfToolsService:
    """插件管理服务：list / load / unload（**不抛异常**，错误以结构化字典返回）。"""

    def __init__(
        self,
        ctx: Context,
        *,
        catalog: dict[str, Callable[[], object]],
        unloadable: tuple[str, ...],
        registry: Any,
    ) -> None:
        self._ctx = ctx
        self._catalog = dict(catalog)
        self._unloadable = frozenset(unloadable)
        # 工具注册表由 tools 插件提供；tools 重建时本插件会随之重装（inject 驱动），
        # 所以这里拿到的一定是当前这一份。
        self._registry = registry

    # ------------------------------------------------------------------
    # 清单（只读）
    # ------------------------------------------------------------------

    @property
    def loadable(self) -> list[str]:
        return sorted(self._catalog)

    @property
    def unloadable(self) -> list[str]:
        return sorted(self._unloadable)

    def list_plugins(self) -> dict[str, Any]:
        """插件台账 + 白名单 + 当前工具面（与 ctx.plugins 同源，永不发岔）。"""
        return {
            "plugins": [
                {
                    "name": info.name,
                    "status": info.status,
                    "inject": list(info.inject),
                    "missing": list(info.missing),
                    "can_unload": info.name in self._unloadable,
                }
                for info in self._ctx.plugins
            ],
            "loadable": self.loadable,
            "unloadable": self.unloadable,
            "tools": list(self._registry.names),
        }

    # ------------------------------------------------------------------
    # 装载 / 卸载
    # ------------------------------------------------------------------

    def load(self, name: str) -> dict[str, Any]:
        """按名字装载白名单里的插件；返回"发生了什么"的结构化结果。"""
        factory = self._catalog.get(name)
        if factory is None:
            return {
                "error": f"插件不在可装载白名单里：{name}",
                "code": "PLUGIN_NOT_ALLOWED",
                "loadable": self.loadable,
            }
        if any(info.name == name for info in self._ctx.plugins):
            return {"error": f"插件已装载：{name}", "code": "PLUGIN_ALREADY_LOADED"}

        services_before = set(self._ctx.services)
        tools_before = set(self._registry.names)

        try:
            plugin = factory()
        except Exception as exc:  # noqa: BLE001 —— 构造失败也是给模型的输入
            return {"error": f"构造插件失败：{exc}", "code": "PLUGIN_LOAD_FAILED"}

        actual = plugin_name(plugin)
        if actual != name:
            return {
                "error": f"白名单名字与实际插件不符：{name!r} ≠ {actual!r}",
                "code": "PLUGIN_NAME_MISMATCH",
            }

        try:
            self._ctx.load_plugin(plugin)
        except Exception as exc:  # noqa: BLE001 —— 装载失败已由内核回卷，无半装状态
            return {"error": f"装载失败：{exc}", "code": "PLUGIN_LOAD_FAILED"}

        info = next((i for i in self._ctx.plugins if i.name == name), None)
        status = info.status if info is not None else "unknown"
        self._ctx.emit(PLUGIN_LOADED, name=name, status=status)

        added_services = sorted(set(self._ctx.services) - services_before)
        added_tools = sorted(set(self._registry.names) - tools_before)
        result: dict[str, Any] = {
            "loaded": name,
            "status": status,
            "services_added": added_services,
            "tools_added": added_tools,
        }
        for key in added_services:  # 有地址的服务（界面 / RPC）顺手告诉模型
            endpoint = _endpoint_of(self._ctx.get(key))
            if endpoint is not None:
                result.setdefault("endpoints", {})[key] = endpoint
        if added_tools:
            result["note"] = "新工具从**下一轮**对话开始出现在工具面里"
        if status == "pending":
            missing = list(info.missing) if info is not None else []
            result["note"] = f"依赖未满足（缺 {missing}），插件处于等待状态"
        return result

    def unload(self, name: str) -> dict[str, Any]:
        """按名字卸载白名单里的插件（依赖者自动退位）；返回结构化结果。"""
        if name not in self._unloadable:
            return {
                "error": f"插件不在可卸载白名单里：{name}",
                "code": "PLUGIN_NOT_ALLOWED",
                "unloadable": self.unloadable,
            }
        if not any(info.name == name for info in self._ctx.plugins):
            return {"error": f"插件未装载：{name}", "code": "PLUGIN_NOT_LOADED"}

        services_before = set(self._ctx.services)
        tools_before = set(self._registry.names)
        try:
            self._ctx.unload_plugin(name)
        except Exception as exc:  # noqa: BLE001 —— 卸载失败同样以结构化错误回给模型
            return {"error": f"卸载失败：{exc}", "code": "PLUGIN_UNLOAD_FAILED"}

        self._ctx.emit(PLUGIN_UNLOADED, name=name)
        return {
            "unloaded": name,
            "services_removed": sorted(services_before - set(self._ctx.services)),
            "tools_removed": sorted(tools_before - set(self._registry.names)),
        }


class SelfToolsPlugin:
    """提供 `self-tools` 服务与三件自扩展工具（inject tools：要登记工具）。"""

    name = "self-tools"
    inject = (TOOLS_SERVICE,)

    def __init__(
        self,
        *,
        catalog: dict[str, Callable[[], object]] | None = None,
        unloadable: tuple[str, ...] | None = None,
    ) -> None:
        self._catalog = dict(catalog) if catalog is not None else default_catalog()
        self._unloadable = (
            tuple(unloadable) if unloadable is not None else DEFAULT_UNLOADABLE
        )

    def setup(self, ctx: Context):
        registry = ctx.require(TOOLS_SERVICE)
        service = SelfToolsService(
            ctx, catalog=self._catalog, unloadable=self._unloadable, registry=registry
        )
        ctx.provide(SELF_TOOLS_SERVICE, service)

        registry.register(
            define_tool(
                "list_plugins",
                "列出本进程的插件台账：每个插件的名字、状态（active/pending/failed）、"
                "依赖、能否卸载，以及可装载/可卸载白名单与当前工具面。只读，无需审批。",
            ),
            service.list_plugins,
        )
        registry.register(
            define_tool(
                "load_plugin",
                "装载一个**可装载白名单**里的插件（清单见 list_plugins 的 loadable）。"
                "装载后它提供的服务与工具从**下一轮**对话开始可用；需要人工审批；"
                "白名单以外的名字会被拒绝。",
                schema_from_model(LoadPluginArgs),
                needs_approval=True,
            ),
            service.load,
        )
        registry.register(
            define_tool(
                "unload_plugin",
                "卸载一个**可卸载白名单**里的插件（清单见 list_plugins 的 unloadable）；"
                "它提供的服务与工具随即消失，依赖它的插件自动退位。需要人工审批。",
                schema_from_model(UnloadPluginArgs),
                needs_approval=True,
            ),
            service.unload,
        )

        def dispose() -> None:
            # 工具注册表在 ctx 之外：撤销要显式写出来（与 tools 插件同一纪律）。
            for tool in ("list_plugins", "load_plugin", "unload_plugin"):
                registry.unregister(tool)

        return dispose


def self_tools_plugin(
    *,
    catalog: dict[str, Callable[[], object]] | None = None,
    unloadable: tuple[str, ...] | None = None,
) -> SelfToolsPlugin:
    """工厂：`catalog` 是"可装载"白名单，`unloadable` 是"可卸载"白名单。"""
    return SelfToolsPlugin(catalog=catalog, unloadable=unloadable)


__all__ = [
    "DEFAULT_CATALOG_NAMES",
    "DEFAULT_UNLOADABLE",
    "LoadPluginArgs",
    "SelfToolsPlugin",
    "SelfToolsService",
    "UnloadPluginArgs",
    "default_catalog",
    "self_tools_plugin",
]
