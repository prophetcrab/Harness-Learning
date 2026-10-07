"""agent_loop —— _03 练习自包含的 Agent 主循环副本（turn/step + 轨迹 + 取消）。

来自 _02 的同名包，复制进 _03 使本练习完整独立。注意：_03 关注点是工具管线，
本包**未经修改**——`AgentLoop` 通过 `execute_tool` 执行器契约工作，而 _03 的
`ToolPipeline.execute()` 正好满足该契约，因此管线可以直接插进主循环而不动它。
这本身就是"接缝"价值的一次验证。

文件分工（推荐阅读顺序）：
1. trace.py   轨迹词汇：TurnResult / Step / ToolResult（turn/step 关系）
2. loop.py    AgentLoop 类：主循环 + 事件回调 + 取消
"""

from __future__ import annotations

from agent_loop.loop import AgentLoop, EventHook, ExecuteTool
from agent_loop.trace import Step, ToolResult, TurnResult, TurnStatus

__all__ = [
    "AgentLoop",
    "EventHook",
    "ExecuteTool",
    "Step",
    "ToolResult",
    "TurnResult",
    "TurnStatus",
]
