"""agent_loop —— _02 练习的包：正式的 Agent 主循环（turn/step + 轨迹 + 取消）。

依赖同目录下的 llm_seam 包（本练习自包含的副本，含 LLMProvider 协议 +
消息词汇 + FakeLLM/DeepSeek），不依赖任何 sibling 练习目录。

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
