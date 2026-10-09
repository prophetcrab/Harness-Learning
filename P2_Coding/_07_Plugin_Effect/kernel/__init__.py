"""kernel —— M6 的第一个机制（`_07` 新增的顶层模块）：ctx + effect + 插件协议。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级；`harness/` 冻结为 M1–M3
基线库，本阶段一行未改。本包机制一句话：

    一切注册都是 effect——注册动作返回一个撤销手柄（disposer），
    卸载（unload）时自动逆序回卷；插件就是 `setup(ctx) -> disposer`。

组件：

- `Context`（context.py）    插件共享的注册中心：命名槽位 provide/retract/require
                            + effect 台账（装载/卸载/自动回卷，LIFO）；
- `Plugin` 协议（plugin.py） 有 `setup(ctx)` 的对象（或可调用对象本身）；
- `load_plugin` / `load_plugins`（plugin.py）  装载器：包成 effect、批量装载
                            并在失败时回卷已装部分。

与既有机制的关系：`_04`–`_06` 的 `ServiceContainer` 是"单槽能力容器"的最小版；
本阶段的 `Context` 是它的正式形态——**能力的注册/回卷统一走 effect 台账**。
`providers/` 里的 `register_*` 插件（providers/plugins.py）把 fs/subprocess
装进 ctx；`context/`（提示词侧）不依赖 kernel，保持互不相干。

依赖方向：`kernel → 无`（纯逻辑，可被任何地方引用）。
"""

from kernel.context import (
    Context,
    Disposer,
    DuplicateSlotError,
    Install,
    UnknownEffectError,
)
from kernel.plugin import Plugin, SetupFn, load_plugin, load_plugins

__all__ = [
    # 注册中心
    "Context",
    "DuplicateSlotError",
    "UnknownEffectError",
    "Install",
    "Disposer",
    # 插件协议与装载器
    "Plugin",
    "SetupFn",
    "load_plugin",
    "load_plugins",
]
