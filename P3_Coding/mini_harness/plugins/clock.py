"""clock 插件 —— 一个**可以被"自扩展工具"装卸**的示例插件（P3 `_01`）。

它不是默认装配的一部分：默认只有"可装载白名单"里留着它的名字。模型调用
`load_plugin("clock")` 才真正把它装进进程，那一刻：

- **服务**：多出 `clock`（一个 `ClockService`）；
- **工具面**：多出 `now`（返回当前本地时间）——从**下一轮**对话开始模型可见；
- 卸载时反过来：`now` 从注册表注销、`clock` 服务撤销（effect 自动回卷 + 本插件
  显式注销工具，后者是因为工具注册表在 ctx 之外，见模块说明）。

为什么选"时钟"当示例：它自包含、只读、无副作用，装卸它不会破坏任何东西——
这是白名单默认放行它的原因（对比：卸载 webui 会切断正在使用的网页连接）。
"""

from __future__ import annotations

from datetime import datetime

from components.tools import define_tool
from kernel import Context
from plugins.services import CLOCK_SERVICE, TOOLS_SERVICE


class ClockService:
    """clock 插件提供的服务（顺手记下自己被装载的时刻）。"""

    def __init__(self) -> None:
        self.loaded_at = datetime.now().isoformat(timespec="seconds")

    def now(self) -> str:
        """当前本地时间（ISO 格式，秒精度）。"""
        return datetime.now().isoformat(timespec="seconds")


class ClockPlugin:
    """装载它 → 多一个 `now` 工具；卸载它 → 工具消失、服务撤销。"""

    name = "clock"
    #: 应用角色（other）：演示插件：演示装载即扩展。
    category = "other"
    inject = (TOOLS_SERVICE,)  # 要登记工具，所以依赖工具面服务

    def setup(self, ctx: Context):
        service = ClockService()
        ctx.provide(CLOCK_SERVICE, service)  # 注册即 effect：卸载自动撤销

        registry = ctx.require(TOOLS_SERVICE)
        registry.register(
            define_tool(
                "now",
                "返回当前本地时间（ISO 格式）。这是一个由 clock 插件提供的工具——"
                "装载 clock 后它才存在。",
            ),
            lambda: {"now": service.now(), "loaded_at": service.loaded_at},
        )

        def dispose() -> None:
            # 工具注册表在 ctx 之外（tools 插件的产物），撤销要显式写出来。
            registry.unregister("now")

        return dispose


def clock_plugin() -> ClockPlugin:
    """工厂（自扩展工具的白名单里用的是它）。"""
    return ClockPlugin()


__all__ = ["ClockPlugin", "ClockService", "clock_plugin"]
