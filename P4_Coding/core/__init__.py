"""core —— 基座（**非插件只有这一处**）：框架 + 引导。

边界（决策 0042）。core 里两个角色、两级纪律：

    core · 框架   插件世界的「物理定律」——服务槽位、事件、effect 回卷、
                  单插件生命周期、变更锁、快照原语。
                  **不认识任何插件**（一律不 import plugins），
                  **不出现任何 harness 词汇**（llm / tool / session / …）。
    core · 引导   自带启动器（`launcher.py`，步 8 长出）：唯一**组合根**——
                  读装配单 → 挂上 loader → 产品政策检查 → `--dump`。
                  它是"先有鸡才能装鸡"的那一下，认识 loader 一个插件
                  （函数内懒加载，全仓唯一的向上边）。

按小步纪律，core · 框架一次只长一条语义（M1 拆步表，见 PLAN.md）：

    步 1  服务槽位 ✅             provide / get / require / retract
    步 2  插件装载与依赖驱动 ✅    inject / optional；pending / active / disabled / failed
    步 3  effect 回卷 ✅          注册即回卷：子先父后、一个失败不打断其余、失败不留半装
    步 4  事件 ✅                 on / emit（广播）/ waterfall（中间件链）；订阅随 effect 回卷
    步 7  变更锁 + revision ✅    公共操作串行化；每次有效变更 revision +1 + 变更报告
    步 8  引导（launcher）✅      自带启动器：boot(装配单) → Runtime；产品政策检查
    （条目 / 树 / 装配单 / 热插拔都是 loader 插件的事，不在 core 的框架部分。）
"""

from core.context import (
    PLUGIN_EFFECT_PREFIX,
    ChangeReport,
    Context,
    DuplicateEffectError,
    DuplicatePluginError,
    DuplicateSlotError,
    EventHandler,
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
    plugin_injects,
    plugin_name,
    plugin_optionals,
    plugin_setup,
)

__all__ = [
    "APPLICATION_ROLE",
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
    "plugin_injects",
    "plugin_name",
    "plugin_optionals",
    "plugin_setup",
]
