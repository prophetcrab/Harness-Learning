"""工具构件 —— 注册表 + pre/execute/post 管线 + 审批策略 + 参数模型 + 计算器。

M2（P1 `_03`）的管线与 P2 `_11` 的参数模型，原样沿用；
P3 只加了一处：注册表支持 `unregister`（插件卸载要能回卷工具面）。
"""

from components.tools.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    AutoApprove,
    AutoDeny,
    PromptApprover,
    ScriptedApprover,
)
from components.tools.arg_models import (
    CalculateArgs,
    EditFileArgs,
    FindFilesArgs,
    ListFilesArgs,
    ReadFileArgs,
    SearchTextArgs,
    ShellArgs,
    WriteFileArgs,
)
from components.tools.calculate import calculate
from components.tools.middlewares import DenyTools, RequireApproval, TruncateOutput
from components.tools.pipeline import ToolCallContext, ToolMiddleware, ToolPipeline, ToolResult
from components.tools.registry import ToolRegistry, define_tool
from components.tools.schema import schema_from_model

__all__ = [
    "ApprovalDecision",
    "ApprovalPolicy",
    "ApprovalRequest",
    "AutoApprove",
    "AutoDeny",
    "CalculateArgs",
    "DenyTools",
    "EditFileArgs",
    "FindFilesArgs",
    "ListFilesArgs",
    "PromptApprover",
    "ReadFileArgs",
    "RequireApproval",
    "ScriptedApprover",
    "SearchTextArgs",
    "ShellArgs",
    "ToolCallContext",
    "ToolMiddleware",
    "ToolPipeline",
    "ToolRegistry",
    "ToolResult",
    "TruncateOutput",
    "WriteFileArgs",
    "calculate",
    "define_tool",
    "schema_from_model",
]
