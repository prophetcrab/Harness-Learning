"""服务名常量 —— 插件之间唯一的"接口词汇"。

谁提供什么服务、谁依赖什么服务，全靠这些字符串约定（与 dsh 的
`ctx.<service>` 命名一脉相承）。集中在这里，避免各插件到处拼字符串。
"""

#: LLM provider（一个满足 LLMProvider 协议的对象）。
LLM_SERVICE = "llm"
#: 文件系统能力（FileSystem 协议）。
FS_SERVICE = "fs"
#: 命令执行能力（SubprocessService 协议）。
SUBPROCESS_SERVICE = "subprocess"
#: 工具面（ToolRegistry：模型可见的工具集合）。
TOOLS_SERVICE = "tools"
#: 会话存储（打开/恢复/列举/分叉会话日志）。
SESSIONS_SERVICE = "sessions"
#: 提示词渲染（section 装配 + 运行时变量 + 装配单）。
PROMPT_SERVICE = "prompt"
#: agent 服务（对外入口：ask 一个 turn）。
AGENT_SERVICE = "agent"
#: 浏览器界面（本机 HTTP 服务；装载即起、卸载即停）。
WEBUI_SERVICE = "webui"
#: JSON-RPC 服务端（stdio / TCP；initialize / session.prompt / session.follow）。
RPC_SERVICE = "rpc"
#: 自扩展工具的服务面（list / load / unload；三件工具是它的薄包装）。
SELF_TOOLS_SERVICE = "self-tools"
#: 示例插件 clock 提供的服务（演示"装载即扩展、卸载即回卷"）。
CLOCK_SERVICE = "clock"
#: 审批策略（浏览器手动审批；见 plugins/web_approval.py）。
APPROVAL_SERVICE = "approval"
#: Token 用量服务的只读地址（`_03`；页面据此挂载用量入口）。
TOKEN_USAGE_SERVICE = "token-usage"
#: 上下文仪表服务的只读地址（`_04`；页面据此挂载仪表入口）。
CONTEXT_METER_SERVICE = "context-meter"
#: 上下文压缩器（`_05`；agent 插件注入它，卸载 = 不再压缩）。
COMPACTER_SERVICE = "compacter"
#: 任务清单服务（`_06`；会话级 todo 清单，整表快照存日志）。
TODOS_SERVICE = "todos"

__all__ = [
    "AGENT_SERVICE",
    "APPROVAL_SERVICE",
    "COMPACTER_SERVICE",
    "CONTEXT_METER_SERVICE",
    "TODOS_SERVICE",
    "TOKEN_USAGE_SERVICE",
    "CLOCK_SERVICE",
    "FS_SERVICE",
    "LLM_SERVICE",
    "PROMPT_SERVICE",
    "RPC_SERVICE",
    "SELF_TOOLS_SERVICE",
    "SESSIONS_SERVICE",
    "SUBPROCESS_SERVICE",
    "TOOLS_SERVICE",
    "WEBUI_SERVICE",
]
