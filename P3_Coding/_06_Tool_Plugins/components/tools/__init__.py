"""工具构件 —— 注册表 + pre/execute/post 管线 + 审批策略 + 声明式工具。

M2（P1 `_03`）的管线与 P2 `_11` 的参数模型，原样沿用。P3 加了三处：

- 注册表支持 `unregister`（插件卸载要能回卷工具面）；
- 注册表带 **owner 署名**与**停用开关**（`_06`：工具面要能被看见、被管理）；
- `declarative.tool`：把一个普通函数就地变成工具（签名即参数表）——
  一件小工具只需**一处**声明，这是"模型自己写工具"能成立的前提。
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
from components.tools.declarative import DeclarativeTool, tool
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
    "DeclarativeTool",
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
    "tool",
]
