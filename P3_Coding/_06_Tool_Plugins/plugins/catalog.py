"""catalog —— "可以装载哪些插件"的唯一事实来源（共享声明模块）。

**为什么单独一个模块**：这件事有两个消费者——

    plugins/self_tools.py    模型通过 `load_plugin` 工具装载
    plugins/plugin_admin.py  人在界面上一键安装

两个各写一份白名单必然漂移（一个加了 `clock` 另一个不知道）。所以放这里，
两边 import 同一份。

**`_06` 多了一份名单**：`TOOL_PLUGIN_NAMES`——"工具插件"这一类。它的消费者是：

    plugins/tools.py（tool-admin）  "可安装"格子里列哪些
    plugins/plugin_admin.py         插件台账里它们照常显示（同一批插件）
    plugins/boot.py                 默认装配装哪几个

**为什么工具插件与示例插件分成两个元组**（而不是合成一张带标记的表）：两者的
语义不同——`DEFAULT_CATALOG_NAMES` 是"装配方预先批准、装了不伤人"的示例；
`TOOL_PLUGIN_NAMES` 是"提供工具能力、可按需开关"的一类。工具管理面板只该显示
后者（把 `clock` 列进"还能装的工具插件"是错的：它不是工具插件）。两个元组、
共用同一张工厂表——语义各归各的，也不会漂移。

**为什么不是插件**：它不提供任何服务、没有生命周期，只是"有哪些可选项"的
一张声明表——与 `events.py` / `services.py` 同类（纯常量/纯声明）。
判定标准还是那条：**能不能独立存在**。它不能，所以它不是插件。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: 默认目录里的示例插件名（顺序即界面上的展示顺序）。
DEFAULT_CATALOG_NAMES: tuple[str, ...] = ("clock", "webui", "rpc")

#: **工具插件**（`_06`）：提供工具能力、可独立装卸的一类插件。
#: 顺序即工具管理面板"可安装"格子的展示顺序。
TOOL_PLUGIN_NAMES: tuple[str, ...] = (
    "tool-calc",
    "tool-fs",
    "tool-search",
    "tool-shell",
)

#: 装载工厂：名字 → 造一个插件对象。
PluginFactory = Callable[[], Any]


def default_catalog() -> dict[str, PluginFactory]:
    """默认可装载的插件（示例三个 + 工具插件四个）。

    工厂里的参数是**部署决策**，写在这里：
      - webui 的端口交给系统分配（port=0）——装配即起、卸载即停；
      - rpc 的 TCP 监听同样交给系统分配（listen=0）；
      - tool-shell 的工作目录/期限/输出上限在装配处定死（模型改不了）；
      - clock 无参数。
    """
    from plugins.clock import clock_plugin
    from plugins.rpc import rpc_plugin
    from plugins.tool_calc import tool_calc_plugin
    from plugins.tool_fs import tool_fs_plugin
    from plugins.tool_search import tool_search_plugin
    from plugins.tool_shell import tool_shell_plugin
    from plugins.webui import webui_plugin

    return {
        "clock": clock_plugin,
        # 界面：端口交给系统分配（port=0），装配即起、卸载即停
        "webui": lambda: webui_plugin(port=0),
        # RPC：TCP 监听同样交给系统分配（listen=0）
        "rpc": lambda: rpc_plugin(listen=0),
        # 工具插件（`_06`）：一件/一类工具一个插件
        "tool-calc": tool_calc_plugin,
        "tool-fs": tool_fs_plugin,
        "tool-search": tool_search_plugin,
        "tool-shell": tool_shell_plugin,
    }


__all__ = [
    "DEFAULT_CATALOG_NAMES",
    "PluginFactory",
    "TOOL_PLUGIN_NAMES",
    "default_catalog",
]
