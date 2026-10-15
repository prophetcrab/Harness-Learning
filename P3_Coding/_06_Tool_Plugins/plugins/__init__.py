"""plugins —— 能力插件层：把 components 里的构件装成 ctx 服务。

规则只有两条：

1. **插件 = 有 setup(ctx) 的对象 + name / inject 声明**（kernel.plugin）；
2. **setup 里做的事全部注册即 effect**——提供的服务、订阅的事件在卸载时
   自动回卷；注册进**外部对象**（如 ToolRegistry）的东西由插件自己写
   dispose 撤销（见 plugins/tools.py 的范例）。

标准插件一览（服务名见 services.py）：

    llm        提供 `llm`（FakeLLM / DeepSeek；带请求瀑布与前后事件）
    fs         提供 `fs`（LocalFS / MemoryFS / WorkspaceJailFS）
    proc       提供 `subprocess`（LocalSubprocess / ScriptedSubprocess；可选）
    tools      ★ 提供 `tools`（**空的** ToolRegistry：工具面底座）+ 工具管理
               （`_06` 拆分：工具内容搬到 tool-* 插件；底座不再是"工具"本身）
    tool-*     ★ 工具插件（`_06`）：tool-calc / tool-fs / tool-search / tool-shell
               各 inject tools（+ 自己需要的能力），登记工具即它们的全部职责
    sessions   提供 `sessions`（打开即恢复 + 崩溃修复 + 分叉；append 时发 `session/event`）
    prompt     提供 `prompt`（section 装配 + 变量瀑布 + 装配单）
    agent      提供 `agent`（会话管理：ask 一个 turn；inject 以上四个）
    webui      提供 `webui`（浏览器界面；inject agent/sessions；装载即起、卸载即停）
    rpc        提供 `rpc`（JSON-RPC 服务端：initialize / prompt / follow；stdio / TCP）
    self-tools ★ 提供 `self-tools`（`_01` 的机制：三件自扩展工具，模型装卸自身插件；
               白名单限定 + 装卸需审批 + 留痕；inject tools）
    clock      ★ 示例可装卸插件（不是默认装配的一部分；装载它才多出 `now` 工具）

装配入口在 boot.py：`boot_offline` / `boot_realtime` / `boot`
（`self_tools=True` 时额外装上自扩展工具）。
"""

from components.context.tokens import (
    DEFAULT_CONTEXT_WINDOW,
    estimate_tokens,
    measure_parts,
)
from plugins.agent import AgentPlugin, AgentService, HarnessSession
from plugins.boot import (
    INTERFACE_ROLES,
    Booted,
    boot,
    boot_offline,
    boot_realtime,
    tool_plugins,
)
from plugins.catalog import TOOL_PLUGIN_NAMES
from plugins.clock import ClockPlugin, ClockService, clock_plugin
from plugins.compact import CompactPlugin, compact_plugin
from plugins.context_meter import (
    ContextMeter,
    ContextMeterEndpoint,
    ContextMeterPlugin,
    ContextSnapshot,
    context_meter_plugin,
)
from plugins.events import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    LLM_REQUEST,
    LLM_RESPONSE,
    LOOP_EVENT,
    PLUGIN_LOADED,
    PLUGIN_UNLOADED,
    PROMPT_VARIABLES,
    SESSION_EVENT,
    SESSION_OPENED,
    TURN_END,
    TURN_START,
)
from plugins.fs import FsPlugin, jail_fs_plugin, local_fs_plugin, memory_fs_plugin
from plugins.llm import (
    OFFLINE_DEMO_REPLY,
    LlmPlugin,
    RepeatingLLM,
    deepseek_llm_plugin,
    fake_llm_plugin,
    offline_llm_plugin,
)
from plugins.plugin_admin import (
    PluginAdminPlugin,
    PluginAdminService,
    plugin_admin_plugin,
)
from plugins.proc import ProcPlugin, local_proc_plugin, scripted_proc_plugin
from plugins.prompt import PromptPlugin, PromptService
from plugins.rpc import (
    EVENT_NOTIFICATION,
    RpcPlugin,
    RpcService,
    rpc_plugin,
    serve_stdio,
)
from plugins.self_tools import (
    DEFAULT_CATALOG_NAMES,
    DEFAULT_UNLOADABLE,
    SelfToolsPlugin,
    SelfToolsService,
    default_catalog,
    self_tools_plugin,
)
from plugins.services import (
    AGENT_SERVICE,
    APPROVAL_SERVICE,
    CLOCK_SERVICE,
    COMPACTER_SERVICE,
    CONTEXT_METER_SERVICE,
    FS_SERVICE,
    LLM_SERVICE,
    PLUGIN_ADMIN_SERVICE,
    PROMPT_SERVICE,
    RPC_SERVICE,
    SELF_TOOLS_SERVICE,
    SESSIONS_SERVICE,
    SUBPROCESS_SERVICE,
    TOKEN_USAGE_SERVICE,
    TOOLS_ADMIN_SERVICE,
    TOOLS_SERVICE,
    WEBUI_SERVICE,
)
from plugins.sessions import ObservableSession, SessionsPlugin, SessionsService
from plugins.token_usage import (
    TokenUsagePlugin,
    UsageEndpoint,
    UsageLedger,
    UsageRecord,
    token_usage_plugin,
)
from plugins.tool_calc import ToolCalcPlugin, tool_calc_plugin
from plugins.tool_fs import ToolFsPlugin, tool_fs_plugin
from plugins.tool_search import ToolSearchPlugin, tool_search_plugin
from plugins.tool_shell import ToolShellPlugin, tool_shell_plugin
from plugins.tools import ToolsAdminService, ToolsPlugin, tools_plugin
from plugins.web_approval import (
    WebApprovalPlugin,
    WebApprovalPolicy,
    web_approval_plugin,
)
from plugins.webui import WebUiPlugin, WebUiService, webui_plugin

__all__ = [
    "AGENT_SERVICE",
    "APPROVAL_REQUESTED",
    "APPROVAL_RESOLVED",
    "APPROVAL_SERVICE",
    "CLOCK_SERVICE",
    "COMPACTER_SERVICE",
    "CONTEXT_METER_SERVICE",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_CATALOG_NAMES",
    "DEFAULT_UNLOADABLE",
    "EVENT_NOTIFICATION",
    "FS_SERVICE",
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "LLM_SERVICE",
    "LOOP_EVENT",
    "OFFLINE_DEMO_REPLY",
    "PLUGIN_LOADED",
    "PLUGIN_UNLOADED",
    "PLUGIN_ADMIN_SERVICE",
    "PROMPT_SERVICE",
    "PROMPT_VARIABLES",
    "RPC_SERVICE",
    "SELF_TOOLS_SERVICE",
    "TOKEN_USAGE_SERVICE",
    "SESSIONS_SERVICE",
    "SESSION_EVENT",
    "SESSION_OPENED",
    "SUBPROCESS_SERVICE",
    "TOOLS_ADMIN_SERVICE",
    "TOOLS_SERVICE",
    "TOOL_PLUGIN_NAMES",
    "TURN_END",
    "TURN_START",
    "WEBUI_SERVICE",
    "AgentPlugin",
    "AgentService",
    "Booted",
    "INTERFACE_ROLES",
    "ClockPlugin",
    "CompactPlugin",
    "ContextMeter",
    "ContextMeterEndpoint",
    "ContextMeterPlugin",
    "ContextSnapshot",
    "ClockService",
    "FsPlugin",
    "HarnessSession",
    "LlmPlugin",
    "ObservableSession",
    "PluginAdminPlugin",
    "PluginAdminService",
    "ProcPlugin",
    "PromptPlugin",
    "PromptService",
    "RepeatingLLM",
    "RpcPlugin",
    "RpcService",
    "SelfToolsPlugin",
    "SelfToolsService",
    "SessionsPlugin",
    "SessionsService",
    "TokenUsagePlugin",
    "ToolCalcPlugin",
    "ToolFsPlugin",
    "ToolSearchPlugin",
    "ToolShellPlugin",
    "ToolsAdminService",
    "ToolsPlugin",
    "UsageEndpoint",
    "UsageLedger",
    "UsageRecord",
    "WebApprovalPlugin",
    "WebApprovalPolicy",
    "WebUiPlugin",
    "WebUiService",
    "boot",
    "boot_offline",
    "boot_realtime",
    "clock_plugin",
    "tool_plugins",
    "compact_plugin",
    "context_meter_plugin",
    "deepseek_llm_plugin",
    "default_catalog",
    "estimate_tokens",
    "fake_llm_plugin",
    "jail_fs_plugin",
    "local_fs_plugin",
    "local_proc_plugin",
    "measure_parts",
    "memory_fs_plugin",
    "offline_llm_plugin",
    "plugin_admin_plugin",
    "rpc_plugin",
    "scripted_proc_plugin",
    "self_tools_plugin",
    "token_usage_plugin",
    "tool_calc_plugin",
    "tool_fs_plugin",
    "tool_search_plugin",
    "tool_shell_plugin",
    "tools_plugin",
    "web_approval_plugin",
    "serve_stdio",
    "webui_plugin",
]
