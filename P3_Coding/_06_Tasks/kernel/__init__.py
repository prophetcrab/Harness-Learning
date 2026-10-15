"""kernel —— 插件内核：ctx（服务 / 事件 / effect）+ 插件生命周期（inject）。

P3 基础包的地基。全部是纯逻辑：不 import components / plugins 里的任何东西，
也不认识 harness 的任何词汇——它只知道"服务名、事件名、插件、effect"。

    Context      注册中心：服务槽位、事件总线、effect 台账、插件台账
    Plugin       插件协议与声明（name / inject / setup）——见 plugin.py

一句话概括行为：**注册即 effect，卸载自动回卷；依赖由 inject 声明，
装载顺序无关。** 细节见 context.py 顶部说明。
"""

from kernel.context import (
    Context,
    Disposer,
    DuplicatePluginError,
    DuplicateSlotError,
    EventHandler,
    Install,
    PluginInfo,
    PluginStatus,
    UnknownEffectError,
    UnknownPluginError,
)
from kernel.plugin import (
    Plugin,
    SetupFn,
    plugin_injects,
    plugin_name,
    plugin_optionals,
    plugin_setup,
)

__all__ = [
    "Context",
    "Disposer",
    "DuplicatePluginError",
    "DuplicateSlotError",
    "EventHandler",
    "Install",
    "Plugin",
    "PluginInfo",
    "PluginStatus",
    "SetupFn",
    "UnknownEffectError",
    "UnknownPluginError",
    "plugin_injects",
    "plugin_name",
    "plugin_optionals",
    "plugin_setup",
]
