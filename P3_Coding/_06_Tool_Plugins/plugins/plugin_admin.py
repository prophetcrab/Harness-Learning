"""plugin-admin 插件 —— 插件管理（分类台账 + 一键安装/卸载/停用/启用）。

**策略住在这里，界面保持哑**：壳只渲染插件自述的 `actions`（每个插件当前
允许做什么），"base 不可卸载""没装 admin 就退化成只读"这类规则全在本模块。

分类（`kernel/plugin.py` 的 `PLUGIN_CATEGORIES`）：

    base        基础：当前应用不可卸载的最小集合（卸了应用不成立）
    capability  能力：提供额外能力（工具 / 观测 / 压缩……），可按需开关
    other       其他：演示、示例插件

**"安装"从哪来**：白名单在 `plugins/catalog.py`——与模型侧 `self_tools` 的
`load_plugin` **同一份**，两个消费者不会漂移。

**人点按钮不需要审批**：审批机制（`_01`）存在是为了检查**模型**发起的操作；
人在界面上亲手点的那一下，本人就是审批人。所以本插件的动作**不经过
approval 服务**——这是刻意的，写在这里免得被当成漏掉了。

**停用 vs 卸载**（内核语义，见 kernel/context.py）：

    停用 → effect 回卷、注册保留；启用在**同一个插件对象**上重跑 setup
    卸载 → 从台账删掉；要回来必须重新装配

**本插件不装载自己的 HTTP 服务**：它把端点作为 `routes` 自述交给壳
（`components/shell/routes.py`）——壳负责挂载与传输（分块/分帧/收尾）。
这样"插件管理"零额外端口，也顺手验证了路由自述这条路真的能用。
"""

from __future__ import annotations

from typing import Any

from components.shell import AdminSpec, RouteRequest, RouteSpec, Surface
from kernel import Context
from plugins.catalog import DEFAULT_CATALOG_NAMES, PluginFactory, default_catalog
from plugins.services import PLUGIN_ADMIN_SERVICE

#: 分类的中文标签与说明（页面直接显示，不必自己解释内核词汇）。
CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "base": ("基础插件", "当前应用的最小集合——卸载/停用它们应用就不成立"),
    "capability": ("能力插件", "提供额外能力（工具、观测、压缩……），可按需开关"),
    "other": ("其他插件", "演示与示例；不参与应用成立与否"),
}

#: 每个动作的中文文案（按钮上显示）。
ACTION_LABELS: dict[str, str] = {
    "install": "安装",
    "unload": "卸载",
    "disable": "停用",
    "enable": "启用",
}


class PluginAdminService:
    """插件管理面：分类台账 + 四个动作（安装 / 卸载 / 停用 / 启用）。

    所有动作都返回 `{"ok": bool, ...}`——**失败也返回人话**，不抛异常给界面
    （与 `/goal` 的人命令同一个原则：人打错/点错了要看到原因，不是 traceback）。
    """

    def __init__(
        self,
        ctx: Context,
        *,
        catalog: dict[str, PluginFactory] | None = None,
        catalog_order: tuple[str, ...] = DEFAULT_CATALOG_NAMES,
    ) -> None:
        self._ctx = ctx
        self._catalog = catalog if catalog is not None else default_catalog()
        self._catalog_order = tuple(
            name for name in catalog_order if name in self._catalog
        )

    # ------------------------------------------------------------------
    # 读：分类台账
    # ------------------------------------------------------------------

    def _actions_for(self, info: Any, *, in_catalog: bool) -> tuple[list[str], str]:
        """这个插件当前允许哪些动作？返回 (动作列表, 被挡的原因)。

        **策略全在这里**——界面只把返回值渲染成按钮，自己不判断。
        """
        if info.category == "base":
            if info.status == "active":
                return [], "基础插件：当前应用不可卸载（卸了应用就不成立）"
            return [], "基础插件：依赖未就绪，等它自己激活"

        if info.status == "active":
            return ["disable", "unload"], ""
        if info.status == "disabled":
            return ["enable", "unload"], ""
        if info.status == "failed":
            # 失败也是"注册着但没跑起来"：让用户能重试（enable 会重跑 setup）
            return ["enable", "unload"], ""
        # pending：依赖没齐，装着呢但没起来——不能停用（已经没在跑），只能卸载
        missing = "、".join(info.missing) or "（未知）"
        return ["unload"], f"等待依赖：{missing}"

    def snapshot(self) -> dict[str, Any]:
        """给界面的完整视图：按分类分组的插件 + 还能安装的插件。"""
        infos = self._ctx.plugins
        loaded = {info.name for info in infos}
        groups: dict[str, list[dict[str, Any]]] = {key: [] for key in CATEGORY_LABELS}
        for info in infos:
            actions, blocked = self._actions_for(info, in_catalog=info.name in self._catalog)
            entry: dict[str, Any] = {
                "name": info.name,
                "status": info.status,
                "category": info.category,
                "inject": list(info.inject),
                "optional": list(info.optional),
                "missing": list(info.missing),
                "missing_optionals": list(info.missing_optionals),
                "actions": actions,
                "blocked_reason": blocked,
                "error": str(info.error) if info.error is not None else "",
            }
            groups.setdefault(info.category, []).append(entry)

        return {
            "groups": [
                {
                    "category": key,
                    "label": label,
                    "hint": hint,
                    "plugins": groups.get(key, []),
                }
                for key, (label, hint) in CATEGORY_LABELS.items()
            ],
            "installable": [
                {"name": name, "actions": ["install"]}
                for name in self._catalog_order
                if name not in loaded
            ],
            "actions": ACTION_LABELS,
            "counts": {
                "total": len(infos),
                "active": sum(1 for i in infos if i.status == "active"),
                "disabled": sum(1 for i in infos if i.status == "disabled"),
            },
        }

    # ------------------------------------------------------------------
    # 写：四个动作
    # ------------------------------------------------------------------

    def install(self, name: str) -> dict[str, Any]:
        """从白名单装载一个插件。"""
        factory = self._catalog.get(name)
        if factory is None:
            allowed = "、".join(self._catalog_order) or "（空）"
            return {"ok": False, "message": f"{name} 不在可安装白名单里（可用：{allowed}）"}
        if name in {info.name for info in self._ctx.plugins}:
            return {"ok": False, "message": f"{name} 已经装载了"}
        try:
            self._ctx.load_plugin(factory())
        except Exception as exc:      # 装载失败要说给人听（人点了按钮）
            return {"ok": False, "message": f"安装 {name} 失败：{exc}"}
        info = next((i for i in self._ctx.plugins if i.name == name), None)
        note = "" if (info is None or info.status == "active") else f"（状态：{info.status}）"
        return {"ok": True, "message": f"已安装 {name}{note}"}

    def unload(self, name: str) -> dict[str, Any]:
        """卸载（从台账删掉）。基础插件被策略挡住。"""
        info = self._info(name)
        if info is None:
            return {"ok": False, "message": f"插件不存在：{name}"}
        if info.category == "base":
            reason = self._actions_for(info, in_catalog=False)[1]
            return {"ok": False, "message": f"不能卸载 {name}：{reason}"}
        try:
            self._ctx.unload_plugin(name)
        except Exception as exc:
            return {"ok": False, "message": f"卸载 {name} 失败：{exc}"}
        return {"ok": True, "message": f"已卸载 {name}"}

    def disable(self, name: str) -> dict[str, Any]:
        """停用：回卷它的能力，但保留注册（一键可恢复）。"""
        info = self._info(name)
        if info is None:
            return {"ok": False, "message": f"插件不存在：{name}"}
        if info.category == "base":
            return {
                "ok": False,
                "message": f"不能停用 {name}：基础插件是当前应用的最小集合"
                "（要换成别的实现，请换装配而不是停用它）",
            }
        try:
            affected = self._ctx.disable_plugin(name)
        except Exception as exc:
            return {"ok": False, "message": f"停用 {name} 失败：{exc}"}
        others = [n for n in affected if n != name]
        tail = f"；依赖它的这些也一起停了：{'、'.join(others)}" if others else ""
        return {"ok": True, "message": f"已停用 {name}{tail}"}

    def enable(self, name: str) -> dict[str, Any]:
        """启用：交回内核判定能否激活（依赖缺失时停在 pending，如实回报）。"""
        info = self._info(name)
        if info is None:
            return {"ok": False, "message": f"插件不存在：{name}"}
        try:
            self._ctx.enable_plugin(name)
        except Exception as exc:
            return {"ok": False, "message": f"启用 {name} 失败：{exc}"}
        after = self._info(name)
        if after is None:
            return {"ok": True, "message": f"已启用 {name}"}
        if after.status == "active":
            return {"ok": True, "message": f"已启用 {name}"}
        missing = "、".join(after.missing) or "（依赖未就绪）"
        return {"ok": True, "message": f"{name} 已启用，但仍在等待依赖：{missing}"}

    def act(self, name: str, action: str) -> dict[str, Any]:
        """按名字分派动作（路由与界面共用这一处）。"""
        handler = {
            "install": self.install,
            "unload": self.unload,
            "disable": self.disable,
            "enable": self.enable,
        }.get(action)
        if handler is None:
            return {"ok": False, "message": f"未知动作：{action}"}
        return handler(name)

    def _info(self, name: str) -> Any:
        return next((i for i in self._ctx.plugins if i.name == name), None)


# ---------------------------------------------------------------------------
# 路由自述：插件管理 API（壳挂载并负责传输）
# ---------------------------------------------------------------------------


def _admin_spec() -> AdminSpec:
    """管理弹层里的「插件」标签页（壳内置的只读页与它同 key，被它顶替）。"""
    return AdminSpec(
        key="plugins",
        label="插件",
        title="插件台账",
        summary="基础插件是当前应用不可卸载的最小集合；点按钮的人自己就是审批人。",
        snapshot="api/plugins",
        action="api/plugins/{name}/{action}",
        foot="<b>人点按钮不需要审批</b>——审批拦的是模型发起的那条路径；"
             "这里的一下由你自己负责。带蓝条的是<b>基础插件</b>："
             "应用成立的最小集合，不可卸载。",
        order=10,
    )


def _admin_routes() -> tuple[RouteSpec, ...]:
    """本插件的两个端点。handler 完全不碰 HTTP（见 modules/shell/routes.py）。"""

    def list_plugins(request: RouteRequest) -> None:
        admin = request.ctx.get(PLUGIN_ADMIN_SERVICE)
        if admin is None:
            request.json({"error": "插件管理服务未就绪"}, status=503)
            return
        request.json(admin.snapshot())

    def act(request: RouteRequest) -> None:
        admin = request.ctx.get(PLUGIN_ADMIN_SERVICE)
        if admin is None:
            request.json({"error": "插件管理服务未就绪"}, status=503)
            return
        name = request.params["name"]
        action = request.params["action"]
        result = admin.act(name, action)
        request.json({**result, "snapshot": admin.snapshot()})

    return (
        RouteSpec("GET", "/api/plugins", list_plugins, order=3),
        RouteSpec("POST", "/api/plugins/<name>/<action>", act, order=4),
    )


class PluginAdminPlugin:
    """提供 `plugin-admin` 服务 + 两个管理端点（inject sessions：快照要读会话无关，仅装配顺序）。"""

    name = "plugin-admin"
    #: 端点要挂到壳上（壳负责传输）；面板按钮不需要。所以不 inject 任何服务——
    #: ctx.plugins / load / unload 都是内核能力，不依赖别的插件。
    category = "capability"

    def __init__(
        self,
        *,
        catalog: dict[str, PluginFactory] | None = None,
        catalog_order: tuple[str, ...] = DEFAULT_CATALOG_NAMES,
    ) -> None:
        self._catalog = catalog
        self._catalog_order = catalog_order

    def setup(self, ctx: Context):
        service = PluginAdminService(
            ctx, catalog=self._catalog, catalog_order=self._catalog_order
        )
        ctx.provide(PLUGIN_ADMIN_SERVICE, service)
        # 界面自述：把两条路由交给壳（壳挂载 + 负责传输），并声明一个管理页
        # （管理弹层里的「插件」标签页）。壳只按 AdminSpec 的通用形状渲染：
        # 标签文字、取数地址、动作模板全部来自这里——壳不认识"插件"二字。
        ctx.provide(
            f"{PLUGIN_ADMIN_SERVICE}.surface",
            Surface(admins=(_admin_spec(),), routes=_admin_routes()),
        )
        return None


def plugin_admin_plugin(
    *,
    catalog: dict[str, PluginFactory] | None = None,
    catalog_order: tuple[str, ...] = DEFAULT_CATALOG_NAMES,
) -> PluginAdminPlugin:
    return PluginAdminPlugin(catalog=catalog, catalog_order=catalog_order)


__all__ = [
    "ACTION_LABELS",
    "CATEGORY_LABELS",
    "PluginAdminPlugin",
    "PluginAdminService",
    "plugin_admin_plugin",
]
