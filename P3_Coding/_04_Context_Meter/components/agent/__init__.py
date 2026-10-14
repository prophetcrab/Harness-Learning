"""agent 构件 —— 主循环（turn/step + 轨迹 + 取消）与轨迹词汇。

P1 `_02`/`_04` 的 AgentLoop 原样沿用：只依赖 LLMProvider 协议，
不 import 任何厂商 SDK；事件流足以重建完整会话日志。
"""

from components.agent.loop import AgentLoop, EventHook, ExecuteTool
from components.agent.trace import Step, ToolResult, TurnResult, TurnStatus

__all__ = [
    "AgentLoop",
    "ExecuteTool",
    "EventHook",
    "Step",
    "ToolResult",
    "TurnResult",
    "TurnStatus",
]
