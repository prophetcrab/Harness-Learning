"""harness —— p4-harness 的公开门面（pip 包的入口）。

    pip install p4-harness
    import harness

    entries = harness.load_manifest("app.yaml", variables={"workspace": "ws"})
    runtime = harness.boot(entries, catalog=my_catalog)
    ...

为什么有这一层：内部包名是 `core`（框架 + 引导），但 `core` 是极通用的
顶层名字，不适合作为对外的 import 名。`harness` 只做 re-export，不增加
任何语义——`harness.boot is core.boot`。

公开边界（0056）：

    · `boot(entries, catalog=...)` 是装配入口；条目可以是列表，也可以是
      `manifests.load` 读出来的 YAML 数据；
    · `Context` / 插件协议辅助函数 / 异常类型是插件作者要用的面；
    · **`import harness` 不会加载任何插件**：core 只认识 `plugins.base.loader`
      这一条向上依赖，且是 `boot()` 里的函数内懒加载；
    · 分发版里带有这套参考插件（base / capability）与装配单，方便开箱即用；
      使用方也可以只用自己的 `catalog`，完全不碰随包插件。

**插件作者不需要 import 本包**：插件协议按鸭子类型读取（name / inject /
optional / requires / setup），一个普通类就是插件。本包只给"装配方"用。
"""

from core import (
    APPLICATION_ROLE,
    INJECT_EFFECT_PREFIX,
    PLUGIN_EFFECT_PREFIX,
    ROLES,
    BootError,
    ChangeReport,
    Context,
    Disposer,
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
    Plugin,
    PluginInfo,
    PluginStatus,
    PolicyError,
    Runtime,
    boot,
    check_product_policy,
    dump,
    matches_plugin_requirement,
    plugin_injects,
    plugin_name,
    plugin_optionals,
    plugin_requires,
    plugin_setup,
)
from manifests.load import ManifestFileError, load_manifest

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
    "ManifestFileError",
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
    "load_manifest",
    "matches_plugin_requirement",
    "plugin_injects",
    "plugin_name",
    "plugin_optionals",
    "plugin_requires",
    "plugin_setup",
]
