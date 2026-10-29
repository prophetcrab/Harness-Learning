"""core —— 插件内核：框架（context / plugin）+ 引导（launcher）。

context.py   服务槽位、effect 回卷、事件、动态依赖、插件生命周期、变更锁
plugin.py    插件协议与声明读取（name / inject / optional / requires / setup）
launcher.py  组合根：boot() 装配插件并返回 Runtime，可选产品政策检查

async-first：生命周期操作都是 awaitable（`await ctx.provide(...)`、
`await ctx.load_plugin(...)`、`await ctx.emit(...)` …）；插件 setup / effect /
事件 handler / inject callback 可同步或异步，内核统一等待。单事件循环模型，
一个 Context 只在一个事件循环里用，非线程安全。

core 不包含插件。boot() 需要调用方传入 catalog，并由一个提供 "loader" 服务的
插件完成装配；该插件不在本目录中（导入处见 launcher.py 的 Runtime.loader 与
boot()）。
"""

from core.context import (
    INJECT_EFFECT_PREFIX,
    PLUGIN_EFFECT_PREFIX,
    ChangeReport,
    Context,
    DuplicateEffectError,
    DuplicatePluginError,
    DuplicateSlotError,
    EventHandler,
    InjectionInfo,
    InjectionStatus,
    Install,
    MissingEffectError,
    MissingPluginError,
    MissingServiceError,
    PluginInfo,
    PluginStatus,
)
from core.launcher import (
    APPLICATION_ROLE,
    ROLES,
    BootError,
    PolicyError,
    Runtime,
    boot,
    check_product_policy,
    dump,
)
from core.plugin import (
    Disposer,
    Plugin,
    matches_plugin_requirement,
    plugin_injects,
    plugin_name,
    plugin_optionals,
    plugin_requires,
    plugin_setup,
)

__all__ = [
    "APPLICATION_ROLE",
    "INJECT_EFFECT_PREFIX",
    "PLUGIN_EFFECT_PREFIX",
    "ROLES",
    "BootError",
    "ChangeReport",
    "Context",
    "Disposer",
    "DuplicateEffectError",
    "DuplicatePluginError",
    "DuplicateSlotError",
    "EventHandler",
    "InjectionInfo",
    "InjectionStatus",
    "Install",
    "MissingEffectError",
    "MissingPluginError",
    "MissingServiceError",
    "Plugin",
    "PluginInfo",
    "PluginStatus",
    "PolicyError",
    "Runtime",
    "boot",
    "check_product_policy",
    "dump",
    "matches_plugin_requirement",
    "plugin_injects",
    "plugin_name",
    "plugin_optionals",
    "plugin_requires",
    "plugin_setup",
]
