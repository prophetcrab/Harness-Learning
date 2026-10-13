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
    tools      提供 `tools`（ToolRegistry；inject fs，可选用 subprocess）
    sessions   提供 `sessions`（打开即恢复 + 崩溃修复 + 分叉）
    prompt     提供 `prompt`（section 装配 + 变量瀑布 + 装配单）
    agent      提供 `agent`（会话管理：ask 一个 turn；inject 以上四个）

装配入口在 boot.py：`boot_offline` / `boot_realtime` / `boot`。
"""

from plugins.agent import AgentPlugin, AgentService, HarnessSession
from plugins.boot import Booted, boot, boot_offline, boot_realtime
from plugins.events import (
    LLM_REQUEST,
    LLM_RESPONSE,
    LOOP_EVENT,
    PROMPT_VARIABLES,
    SESSION_OPENED,
    TURN_END,
    TURN_START,
)
from plugins.fs import FsPlugin, jail_fs_plugin, local_fs_plugin, memory_fs_plugin
from plugins.llm import LlmPlugin, deepseek_llm_plugin, fake_llm_plugin
from plugins.proc import ProcPlugin, local_proc_plugin, scripted_proc_plugin
from plugins.prompt import PromptPlugin, PromptService
from plugins.services import (
    AGENT_SERVICE,
    FS_SERVICE,
    LLM_SERVICE,
    PROMPT_SERVICE,
    SESSIONS_SERVICE,
    SUBPROCESS_SERVICE,
    TOOLS_SERVICE,
)
from plugins.sessions import SessionsPlugin, SessionsService
from plugins.tools import ToolsPlugin

__all__ = [
    "AGENT_SERVICE",
    "FS_SERVICE",
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "LLM_SERVICE",
    "LOOP_EVENT",
    "PROMPT_SERVICE",
    "PROMPT_VARIABLES",
    "SESSIONS_SERVICE",
    "SESSION_OPENED",
    "SUBPROCESS_SERVICE",
    "TOOLS_SERVICE",
    "TURN_END",
    "TURN_START",
    "AgentPlugin",
    "AgentService",
    "Booted",
    "FsPlugin",
    "HarnessSession",
    "LlmPlugin",
    "ProcPlugin",
    "PromptPlugin",
    "PromptService",
    "SessionsPlugin",
    "SessionsService",
    "ToolsPlugin",
    "boot",
    "boot_offline",
    "boot_realtime",
    "deepseek_llm_plugin",
    "fake_llm_plugin",
    "jail_fs_plugin",
    "local_fs_plugin",
    "local_proc_plugin",
    "memory_fs_plugin",
    "scripted_proc_plugin",
]
