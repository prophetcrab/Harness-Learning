"""tool_pipeline —— _03 练习的包：工具注册表 + 审批 + pre/execute/post 管线。

本包是 _01 `llm_seam.tools.Toolbox`（"注册 + 查表 + 错误包装"）的完整升级：
把工具系统的四个关注点拆成各自独立的模块，再在 pipeline 里装配。

文件分工（推荐阅读顺序）：

1. registry.py     定义与实现分离；全局层 + 作用域遮蔽
2. schema.py       用 pydantic 模型声明参数 → JSON Schema
3. approval.py     审批策略协议 + Auto/Deny/Scripted/Prompt 实现（fail-closed）
4. pipeline.py     核心：pre → execute → post 中间件链 + 超时 + ToolResult
5. middlewares.py  示例中间件：DenyTools / RequireApproval / TruncateOutput
6. builtin.py      内置工具 + build_default_registry()

三角色（capability seam）在工具系统里的落地：
- Definition：registry.ToolDefinition（申明"是什么"，不含实现）
- Provider：注册时绑定的实现函数（真实 / 假实现 / 沙箱实现）
- Consumer：pipeline.ToolPipeline.execute()（AgentLoop 唯一依赖的入口）

对应 dsh：`packages/core/tools/src/index.ts` + `docs/tool-execution-pipeline.md`。
"""

from harness.tools.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    AutoApprove,
    AutoDeny,
    PromptApprover,
    ScriptedApprover,
)
from harness.tools.builtin import build_default_registry
from harness.tools.middlewares import DenyTools, RequireApproval, TruncateOutput
from harness.tools.pipeline import (
    PreDecision,
    ToolCallContext,
    ToolMiddleware,
    ToolPipeline,
    ToolResult,
    ToolStatus,
)
from harness.tools.registry import (
    RegisteredTool,
    ToolDefinition,
    ToolFunc,
    ToolRegistry,
    ToolScope,
    define_tool,
)
from harness.tools.schema import schema_from_model

__all__ = [
    # 注册表
    "ToolRegistry",
    "ToolScope",
    "ToolDefinition",
    "RegisteredTool",
    "ToolFunc",
    "define_tool",
    # schema
    "schema_from_model",
    # 审批
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalDecision",
    "AutoApprove",
    "AutoDeny",
    "ScriptedApprover",
    "PromptApprover",
    # 管线
    "ToolPipeline",
    "ToolResult",
    "ToolStatus",
    "ToolCallContext",
    "PreDecision",
    "ToolMiddleware",
    # 中间件与内置
    "DenyTools",
    "RequireApproval",
    "TruncateOutput",
    "build_default_registry",
]
