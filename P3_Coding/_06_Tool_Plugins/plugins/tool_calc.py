"""tool-calc 插件 —— 计算器工具（`_06`：一件工具可以自己成为一个插件）。

**它为什么能单独成为插件**：不依赖任何服务（不要文件系统、不要进程、不要模型），
只提供一件纯函数工具。这正是"工具插件之间解耦"的极端例子——多装它就多一件
工具，卸掉它工具面就少一件，别的一概不受影响。

**写作方式**：`@tool` 声明式（签名即参数表，文档串即说明）。一件小工具的全部
信息就在**一个函数**上，不用另外摆参数模型与 `define_tool` 两处——这条也是
"模型自己写工具"能成立的前提（见 components/tools/declarative.py）。
"""

from __future__ import annotations

from components.tools import calculate, tool
from kernel import Context
from plugins.services import TOOLS_SERVICE

#: 工具名（登记与注销共用一处，避免两处写字符串写岔）。
TOOL_NAME = "calculate"


@tool(TOOL_NAME, description=(
    "计算一个算术表达式并返回精确结果。涉及算术时必须用本工具，不要心算。"
    "支持 + - * / // % ** 与括号，例如 '1234*56.78'。"
))
def calculate_tool(expression: str) -> dict:
    """计算算术表达式并返回精确结果（说明见 description）。"""
    return calculate(expression)


class ToolCalcPlugin:
    """提供 `calculate` 这一件工具（inject tools：要登记进工具面）。"""

    name = "tool-calc"
    #: 应用角色（capability）：可按需装卸的一件能力。
    category = "capability"
    inject = (TOOLS_SERVICE,)

    def setup(self, ctx: Context):
        registry = ctx.require(TOOLS_SERVICE)
        registry.register(calculate_tool.definition, calculate_tool.func, owner=self.name)

        def dispose() -> None:
            # 工具面在 ctx 之外（是 tools 插件的产物）：撤销要显式写出来。
            registry.unregister(TOOL_NAME)

        return dispose


def tool_calc_plugin() -> ToolCalcPlugin:
    """工厂（`plugins/catalog.py` 的白名单里用的是它）。"""
    return ToolCalcPlugin()


__all__ = ["TOOL_NAME", "ToolCalcPlugin", "calculate_tool", "tool_calc_plugin"]
