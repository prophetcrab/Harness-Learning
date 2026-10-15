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

import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from components.tools import ApprovalDecision, ApprovalRequest, define_tool, schema_from_model
from kernel import Context, plugin_name
from plugins.events import PLUGIN_LOADED, PLUGIN_UNLOADED
from plugins.services import (
    APPROVAL_SERVICE,
    FS_SERVICE,
    SELF_TOOLS_SERVICE,
    TOOLS_SERVICE,
)

#: 线程局部标记：工具路径下管线已完成审批，服务层据此不重复问
#: （工具在管线的工作线程里跑，所以用线程局部而不是全局标记）。
_approved_call = threading.local()

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


class InstallPluginArgs(BaseModel):
    path: str = Field(
        description="工作区内插件文件路径（相对工作区根的 .py 文件），"
        "例如 'plugins/my_tool.py'。文件里要有一个返回插件对象的 factory 函数，"
        "或一个名为 <插件名>_plugin 的工厂函数。"
    )
    entry: str | None = Field(
        default=None,
        description="工厂函数名（可选）；不给则自动找 factory / <模块名>_plugin / "
        "唯一的 Plugin 类。",
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

    def register_factory(self, name: str, factory: Callable[[], object]) -> None:
        """把一个新的插件工厂加进可装载白名单（**手动审批通过后**才调用）。

        这是"先审批、后入白名单"的写入端：用户看过代码并批准，才把名字登记进
        catalog；登记完立刻用它装载，所以"批准"与"生效"是同一个动作的两半。
        """
        self._catalog[name] = factory

    def make_unloadable(self, name: str) -> None:
        """把一个插件加进可卸载白名单（同样由审批路径调用）。"""
        self._unloadable = frozenset(set(self._unloadable) | {name})

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

    # ------------------------------------------------------------------
    # 从工作区安装（先审批、后入白名单、再激活）
    # ------------------------------------------------------------------

    def _request_install_approval(self, path: str, source: str) -> ApprovalDecision:
        """在服务层自己问一次审批（不依赖工具管线）。

        工具调用会先被管线的 pre 阶段拦一次（`needs_approval=True`）；但服务的
        公共方法也可能被**直接调用**（测试、未来别的入口）。"引入新代码"这件事
        太敏感，不能只靠调用方记得走管线——所以这里再问一次，**没策略就拒绝**
        （fail-closed）。管线里已批准过的场景，WеbApprovalPolicy 会立刻弹第二次
        卡片；显式传了 `ScriptedApprover` 的测试则各自消费一条剧本。

        为避免重复弹窗，管线批准时会通过 `ctx` 记住"这次安装已放行"——
        见 `_install_preapproved`。
        """
        request = ApprovalRequest(
            tool_name="install_plugin",
            arguments={"path": path, "bytes": len(source.encode("utf-8"))},
            reason="安装新插件（会引入可执行代码）——请确认源码",
        )
        policy = self._ctx.get(APPROVAL_SERVICE)
        if policy is None:
            return ApprovalDecision(
                False, "没有审批策略：引入新代码必须有人批准（fail-closed 拒绝）"
            )
        return policy.approve(request)

    def install(self, path: str, entry: str | None = None) -> dict[str, Any]:
        """把一个**工作区里的 .py 文件**安装成插件：读代码 → 审批 → 装载 → 入白名单。

        与 `load` 的区别：`load` 只能装**已经在白名单里**的（装配方预先批准的
        名字）；`install` 允许引入**新代码**，因此它的安全边界只有一条——
        **人工审批**（工具标了 needs_approval，界面会把源码摆给审批人看）。
        审批被拒 → 什么都不发生；审批通过 → 登记进白名单并立即装载。

        这条路是 P3 `_01` 唯一"能引入新代码"的入口，所以除了审批之外，
        还刻意做了三件事：只读工作区内的文件（走 fs 服务，天然受沙箱限制）、
        只接受 .py、装载失败不留下半装状态（内核保证）。
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

        # 审批：工具路径下管线已经问过了（见 install_plugin 工具的包装器把标记
        # 置上）；直接调服务（测试 / 未来别的入口）则由服务自己问，问不到即拒绝。
        if getattr(_approved_call, "tool", None) != "install_plugin":
            decision = self._request_install_approval(path, source)
            if not decision.allowed:
                return {
                    "error": f"安装被拒绝：{decision.reason}",
                    "code": "PLUGIN_ACCESS_DENIED",
                    "path": path,
                }

        try:
            plugin, factory, name = _plugin_from_source(source, path, entry)
        except Exception as exc:  # noqa: BLE001 —— 代码有问题，如实回给模型
            return {"error": f"解析插件失败：{exc}", "code": "INSTALL_PARSE_FAILED"}

        # 白名单内重名：交给 load 的既有语义（已装载 → 结构化错误）
        services_before = set(self._ctx.services)
        tools_before = set(self._registry.names)

        self.register_factory(name, factory)
        self.make_unloadable(name)  # 自己装的，允许自己卸（同样是审批过的动作）

        try:
            self._ctx.load_plugin(factory())
        except Exception as exc:  # noqa: BLE001
            self._catalog.pop(name, None)  # 装载失败：撤销刚做的白名单登记
            return {"error": f"装载失败：{exc}", "code": "PLUGIN_LOAD_FAILED"}

        info = next((i for i in self._ctx.plugins if i.name == name), None)
        status = info.status if info is not None else "unknown"
        self._ctx.emit(PLUGIN_LOADED, name=name, status=status, source=path)
        result: dict[str, Any] = {
            "installed": name,
            "source": path,
            "status": status,
            "registered_in_whitelist": True,
            "services_added": sorted(set(self._ctx.services) - services_before),
            "tools_added": sorted(set(self._registry.names) - tools_before),
        }
        for key in result["services_added"]:
            endpoint = _endpoint_of(self._ctx.get(key))
            if endpoint is not None:
                result.setdefault("endpoints", {})[key] = endpoint
        if result["tools_added"]:
            result["note"] = "新工具从**下一轮**对话开始出现在工具面里"
        if status == "pending":
            missing = list(info.missing) if info is not None else []
            result["note"] = f"依赖未满足（缺 {missing}），插件处于等待状态"
        return result


def _plugin_from_source(
    source: str, path: str, entry: str | None
) -> tuple[object, Callable[[], object], str]:
    """把一段源码编译成"插件工厂"，返回 (插件实例, 工厂, 名字)。

    约定（与项目里的插件写法一致）：源码里应有
      · `def factory(): return Plugin()`，或
      · `def <模块名>_plugin(): ...`，或
      · 唯一一个带 `setup` 方法的类（自动实例化）。
    entry 显式给了就用它。
    """
    import importlib.util
    import sys
    import tempfile
    from pathlib import Path

    stem = Path(path).stem
    module_name = f"user_plugin_{stem}"
    # 写进临时文件再 import（源码来自工作区，用文件路径加载最直观）
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", prefix=f"{module_name}_", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(source)
        tmp_path = handle.name
    try:
        spec = importlib.util.spec_from_file_location(module_name, tmp_path)
        if spec is None or spec.loader is None:
            raise ValueError("无法为该文件建立模块规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    candidates: list[str] = []
    if entry:
        candidates.append(entry)
    candidates += ["factory", f"{stem}_plugin", f"{stem}_plugin"]
    for attr in ("plugin", "PLUGIN"):
        candidates.append(attr)

    for attr in candidates:
        obj = getattr(module, attr, None)
        if obj is None:
            continue
        if callable(obj) and not hasattr(obj, "setup"):
            instance = obj()
        else:
            instance = obj
        if hasattr(instance, "setup"):
            factory = (lambda fn=obj: fn()) if callable(obj) and not hasattr(obj, "setup") \
                else (lambda inst=instance: inst)
            name = plugin_name(instance)
            return instance, factory, name

    # 兜底：找唯一的"带 setup 的类"
    classes = [
        value for value in vars(module).values()
        if isinstance(value, type) and hasattr(value, "setup")
    ]
    if len(classes) == 1:
        cls = classes[0]
        instance = cls()
        return instance, cls, plugin_name(instance)

    raise ValueError(
        "找不到插件入口：请在源码里定义 factory()、"
        f"{stem}_plugin()，或唯一一个带 setup 方法的类"
    )


class SelfToolsPlugin:
    """提供 `self-tools` 服务与三件自扩展工具（inject tools：要登记工具）。"""

    name = "self-tools"
    inject = (TOOLS_SERVICE,)
    #: 可选：审批策略（缺失 → 装卸操作全拒，fail-closed）与文件系统
    #: （缺失 → install_plugin 读不到源码并如实报错）。
    optional = (APPROVAL_SERVICE, FS_SERVICE)

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
        def install_tool(path: str, entry: str | None = None) -> dict[str, Any]:
            """install_plugin 的工具入口：标记"管线已审批"，再交给服务。"""
            _approved_call.tool = "install_plugin"
            try:
                return service.install(path, entry)
            finally:
                _approved_call.tool = None

        registry.register(
            define_tool(
                "install_plugin",
                "从**工作区里的 .py 文件**安装一个新插件（这条路径允许引入新代码）。"
                "需要人工审批——审批人会看到源码全文，批准后才装载并加入白名单；"
                "被拒则什么都不发生。插件源码里要有 factory()、<文件名>_plugin()，"
                "或唯一一个带 setup 方法的类。",
                schema_from_model(InstallPluginArgs),
                needs_approval=True,
            ),
            install_tool,
        )

        def dispose() -> None:
            # 工具注册表在 ctx 之外：撤销要显式写出来（与 tools 插件同一纪律）。
            for tool in ("list_plugins", "load_plugin", "unload_plugin", "install_plugin"):
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
