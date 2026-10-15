"""catalog —— "可以装载哪些插件"的唯一事实来源（共享声明模块）。

**为什么单独一个模块**：这件事有两个消费者——

    plugins/self_tools.py    模型通过 `load_plugin` 工具装载
    plugins/plugin_admin.py  人在界面上一键安装

两个各写一份白名单必然漂移（一个加了 `clock` 另一个不知道）。所以放这里，
两边 import 同一份。

**为什么不是插件**：它不提供任何服务、没有生命周期，只是"有哪些可选项"的
一张声明表——与 `events.py` / `services.py` 同类（纯常量/纯声明）。
判定标准还是那条：**能不能独立存在**。它不能，所以它不是插件。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: 默认目录里的插件名（顺序即界面上的展示顺序）。
DEFAULT_CATALOG_NAMES: tuple[str, ...] = ("clock", "webui", "rpc")

#: 装载工厂：名字 → 造一个插件对象。
PluginFactory = Callable[[], Any]


def default_catalog() -> dict[str, PluginFactory]:
    """默认可装载的三个插件（都是"装了有用、卸了不伤人"的类型）。

    工厂里的参数是**部署决策**，写在这里：
      - webui 的端口交给系统分配（port=0）——装配即起、卸载即停；
      - rpc 的 TCP 监听同样交给系统分配（listen=0）；
      - clock 无参数。
    """
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


__all__ = ["DEFAULT_CATALOG_NAMES", "PluginFactory", "default_catalog"]
