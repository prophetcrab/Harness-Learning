"""agent_loop —— _04 练习自包含的 Agent 主循环副本（turn/step + 轨迹 + 取消）。

来自 _02 的同名包，复制进 _04 使本练习完整独立。相对 _02 的改动（见 loop.py
顶部说明）：支持 initial_messages / initial_turn（resume 用）、事件负载增强
（完整 tool_calls + call_id + step_end），以便会话日志能完整重建。

文件分工（推荐阅读顺序）：
1. trace.py   轨迹词汇：TurnResult / Step / ToolResult（turn/step 关系）
2. loop.py    AgentLoop 类：主循环 + 事件回调 + 取消
"""

from __future__ import annotations

from harness.agent.loop import AgentLoop, EventHook, ExecuteTool
from harness.agent.trace import Step, ToolResult, TurnResult, TurnStatus

__all__ = [
    "AgentLoop",
    "EventHook",
    "ExecuteTool",
    "Step",
    "ToolResult",
    "TurnResult",
    "TurnStatus",
]
