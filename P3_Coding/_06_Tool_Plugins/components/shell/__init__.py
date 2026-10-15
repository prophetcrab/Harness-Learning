"""shell 契约 —— 插件向界面自述的四件事（面板 / 显示 / 路由 / 管理页）。

界面（webui）不认识任何具体插件：它遍历所有服务，读它们自述的这四样东西。

    panels   我有个页面 → 给我一个按钮（顶栏）
    display  我的事件该怎么画 → 壳按通用渲染器渲染
    routes   我有自己的 HTTP 端点 → 壳挂载并转发
    admins   我有管理面板 → 管理弹层里多一个标签页（`_06`）

四种自述都能挂在服务对象上，或统一挂在 `Surface` 上（当服务对象是 components
层的类、不方便挂属性时）。详见各子模块。
"""

from components.shell.admin import BUILTIN_PLUGINS_TAB, AdminSpec, admins_of
from components.shell.discover import Surface, display_of, panels_of, routes_of
from components.shell.display import RENDERERS, DisplaySpec
from components.shell.panel import BadgeKind, PanelSpec
from components.shell.routes import (
    METHODS,
    FrameWriter,
    RouteRequest,
    RouteResponder,
    RouteSpec,
    RouteTable,
    match_path,
)

__all__ = [
    "BUILTIN_PLUGINS_TAB",
    "METHODS",
    "RENDERERS",
    "AdminSpec",
    "admins_of",
    "BadgeKind",
    "DisplaySpec",
    "FrameWriter",
    "PanelSpec",
    "RouteRequest",
    "RouteResponder",
    "RouteSpec",
    "RouteTable",
    "Surface",
    "display_of",
    "match_path",
    "panels_of",
    "routes_of",
]
