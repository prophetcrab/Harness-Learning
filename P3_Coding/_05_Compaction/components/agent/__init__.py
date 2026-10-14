"""agent 构件 —— 主循环（turn/step + 轨迹 + 取消）与轨迹词汇。

P1 `_02`/`_04` 的 AgentLoop 原样沿用：只依赖 LLMProvider 协议，
不 import 任何厂商 SDK；事件流足以重建完整会话日志。

`_05` 新增 compact（上下文压缩）：策略、切点与摘要请求都是纯函数，
Compactor 只依赖 LLMProvider 协议。
"""

from components.agent.compact import (
    CompactionOutcome,
    CompactionPlan,
    CompactionPolicy,
    Compactor,
    build_summary_request,
    choose_cut,
    plan_compaction,
)
from components.agent.loop import AgentLoop, ContextCompactor, EventHook, ExecuteTool
from components.agent.trace import Step, ToolResult, TurnResult, TurnStatus

__all__ = [
    "AgentLoop",
    "CompactionOutcome",
    "CompactionPlan",
    "CompactionPolicy",
    "Compactor",
    "ContextCompactor",
    "ExecuteTool",
    "EventHook",
    "Step",
    "ToolResult",
    "TurnResult",
    "TurnStatus",
    "build_summary_request",
    "choose_cut",
    "plan_compaction",
]
