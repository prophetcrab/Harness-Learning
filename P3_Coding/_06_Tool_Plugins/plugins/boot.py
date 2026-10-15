"""boot —— 标准装配："一条命令起一个可用的插件化 harness"。

装配方式只有一条规则：**选插件、装进 ctx**。谁先谁后无所谓（inject 驱动），
缺什么服务在装载时报错并指出（fail loud）。

三种常用组合（入口与测试都用它们）：

    boot_offline(...)  全离线：FakeLLM + 内存文件系统 + 剧本命令（零副作用）
    boot_realtime(...) 真实组合：DeepSeek + 真实磁盘 + 真实进程
    boot(...)          自定义：插件列表自定（实验与扩展的入口）

返回 `Booted`：ctx + agent 服务 + 装配说明。**卸载 = ctx.unload_plugin(...)**，
或直接丢掉整个 ctx（进程退出）；每样登记物的回卷都已被 effect 保证。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from components.context.tokens import DEFAULT_CONTEXT_WINDOW
from components.tools import ApprovalPolicy
from kernel import Context
from plugins.agent import AgentPlugin, AgentService
from plugins.compact import compact_plugin
from plugins.context_meter import context_meter_plugin
from plugins.fs import jail_fs_plugin, local_fs_plugin, memory_fs_plugin
from plugins.llm import deepseek_llm_plugin, fake_llm_plugin, offline_llm_plugin
from plugins.plugin_admin import plugin_admin_plugin
from plugins.proc import local_proc_plugin, scripted_proc_plugin
from plugins.prompt import PromptPlugin
from plugins.self_tools import self_tools_plugin
from plugins.services import AGENT_SERVICE
from plugins.sessions import SessionsPlugin
from plugins.token_usage import token_usage_plugin
from plugins.tool_calc import tool_calc_plugin
from plugins.tool_fs import tool_fs_plugin
from plugins.tool_search import tool_search_plugin
from plugins.tool_shell import tool_shell_plugin
from plugins.tools import ToolsPlugin
from plugins.web_approval import web_approval_plugin


@dataclass
class Booted:
    """一次标准装配的产物：ctx + agent 服务。

    `agent` 是**属性**而不是快照：换过插件（卸载/重装）后 agent 服务会被
    重建，从 ctx 现取才能保证拿到当前组合下的那一个。
    """

    ctx: Context

    @property
    def agent(self) -> AgentService:
        """当前的 agent 服务（从 ctx 现取；未激活时 fail loud）。"""
        return self.ctx.require(AGENT_SERVICE)

    @property
    def plugins(self) -> list[str]:
        """当前活跃插件名（按装载顺序）。"""
        return self.ctx.active_plugins

    def close(self) -> None:
        """卸载全部插件（回卷一切登记：服务、事件订阅、工具面……）。

        逆序卸载；顺序无关紧要——每个插件只回卷自己的东西。
        """
        for name in reversed(self.ctx.active_plugins):
            self.ctx.unload_plugin(name)


def tool_plugins(*, workspace: str | Path, shell: bool = True) -> list[Any]:
    """默认的**工具插件**组合（`_06`）：计算 / 读写 / 检索（+ 命令）。

    拆开摆在这里而不是塞回 ToolsPlugin 里，是为了让"默认装哪几件工具"成为
    **装配方一眼可见、可以改**的一行——想少给模型检索能力，就从这里删掉一个；
    装配方也可以完全不用它，自己挑（见 plugins/catalog.py 的白名单）。

    `shell=False` 时不装 tool-shell（连同它依赖的 subprocess 一起省掉）。
    """
    built: list[Any] = [tool_calc_plugin(), tool_fs_plugin(), tool_search_plugin()]
    if shell:
        built.append(tool_shell_plugin(cwd=str(workspace)))
    return built


def boot(
    *,
    plugins: list[Any],
    workspace: str | Path,
    roles: Mapping[str, str] | None = None,
) -> Booted:
    """按给定插件列表装配（顺序无关；inject 决定激活时机）。

    `roles`：**装配方的角色声明表**（插件名 → base/capability/other），覆盖
    插件自述的 `category`。同一个插件在不同应用里可以是不同角色——判断权在
    装配方，见 `kernel/plugin.resolve_category`。

        boot(plugins=[..., webui], roles={"webui": "base"})
        # 这个组合是"界面即产品"：webui 是地基，台账里不给停用/卸载按钮
    """
    ctx = Context()
    ctx.load_plugins(plugins, roles=roles)
    pending = ctx.pending_plugins
    if pending:
        # 待激活残留 = 依赖写错了或漏装插件：静默继续会让"服务不存在"
        # 在运行时才炸；装载期直接暴露（fail loud，铁律 #8）。
        detail = "；".join(f"{name} 缺 {ctx.missing_dependencies(name)}" for name in pending)
        raise RuntimeError(f"以下插件未能激活（依赖不满足）：{detail}")
    return Booted(ctx=ctx)


#: 「界面即产品」的装配角色表：这个组合里，界面不是可选能力而是地基。
#:
#: 判断依据是**卸了它应用还成立吗**：在 `--web` 这种组合里，用户拿到的东西
#: 就是那个网页——没有 webui 就没有入口，"应用"不成立。所以它是 base：
#: 台账里不给停用/卸载按钮（避免"用页面把承载页面的插件关掉"这种自断路径）。
#:
#: 反过来，终端组合（`chat.py` 默认、RPC 组合）不传这张表：那种应用里
#: "说话"靠终端/RPC，webui 只是多一个观察窗口，关掉照常用 → capability。
INTERFACE_ROLES: dict[str, str] = {"webui": "base"}


def boot_offline(
    *,
    root: str | Path,
    workspace: str | Path,
    approval: ApprovalPolicy | None = None,
    max_steps: int | None = None,
    script: list | None = None,
    self_tools: bool = False,
    token_budget: int | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    roles: Mapping[str, str] | None = None,
) -> Booted:
    """全离线装配：离线模型 + 内存文件系统 + 剧本命令。

    - root：会话日志根目录；workspace：文件工具的工作区（内存实现下只是
      "相对路径的根"，不落盘）；
    - script：给了 → **严格剧本**（FakeLLM，用完还调就报错；测试用）；
      没给 → **固定回复、不限轮数**（交互界面/长演示用，人聊多少轮都答得上）；
    - self_tools：装上 `_01` 的自扩展工具（模型可装卸白名单插件）；
    - token_budget：单个 turn 的 token 上限（`_03`；None = 不限，超限以
      `budget_exceeded` 收尾）；
    - context_window：上下文窗口（`_04` 仪表的分母 + `_05` 压缩的阈值基数；
      默认 1M，见 components/context/tokens.py）。
    """
    Path(workspace).mkdir(parents=True, exist_ok=True)
    llm = fake_llm_plugin(script) if script is not None else offline_llm_plugin()
    plugins: list[Any] = [
        llm,
        memory_fs_plugin(),
        scripted_proc_plugin(),
        SessionsPlugin(root),
        PromptPlugin(workspace=workspace),
        # 装配方的审批策略同时交给工具底座：模型"装新工具插件"那条路要用它
        # （没给则现取 optional 的 approval 服务；两者都没有 → fail-closed）。
        ToolsPlugin(approval=approval),
    ]
    plugins.append(token_usage_plugin())   # `_03`：用量账本 + 用量页面（8767）
    plugins.append(plugin_admin_plugin())  # `_05` 收尾：插件管理（分类 + 一键开关）
    plugins.append(context_meter_plugin(capacity=context_window))  # `_04`：仪表（8768）
    plugins.append(compact_plugin(capacity=context_window))        # `_05`：压缩
    plugins += tool_plugins(workspace=workspace)   # `_06`：工具插件
    if self_tools:
        # 装了自扩展工具就一并装浏览器审批：装卸/安装类操作要有人点头，
        # 没有终端可问时由界面弹卡片（无界面时该策略 fail-closed）。
        plugins.append(web_approval_plugin())
        plugins.append(self_tools_plugin())
    plugins.append(AgentPlugin(approval=approval, max_steps=max_steps, token_budget=token_budget))
    return boot(plugins=plugins, workspace=workspace, roles=roles)


def boot_realtime(
    *,
    root: str | Path,
    workspace: str | Path,
    project_root: str | Path,
    approval: ApprovalPolicy | None = None,
    fs: str = "local",
    shell: str = "local",
    max_steps: int | None = None,
    self_tools: bool = False,
    token_budget: int | None = None,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    roles: Mapping[str, str] | None = None,
) -> Booted:
    """真实组合：DeepSeek + 真实磁盘（local/jail）+ 真实进程（local/无）。

    - project_root：读 .env 的位置（缺 key 时 fail loud——真实模式的要求）；
    - fs="local" | "jail"；shell="local" | "none"；
    - self_tools：装上 `_01` 的自扩展工具（模型可装卸白名单插件）；
    - token_budget：单个 turn 的 token 上限（`_03`；None = 不限，超限以
      `budget_exceeded` 收尾）；
    - context_window：同 boot_offline（默认 1M）；
    - roles：装配方的角色声明表（同 boot；如 `INTERFACE_ROLES` 让 webui 变 base）。
    """
    plugins: list[Any] = [deepseek_llm_plugin(project_root)]
    plugins.append(jail_fs_plugin(workspace) if fs == "jail" else local_fs_plugin(workspace))
    if shell == "local":
        plugins.append(local_proc_plugin())
    plugins += [
        SessionsPlugin(root),
        PromptPlugin(workspace=workspace),
        # 装配方的审批策略同时交给工具底座：模型"装新工具插件"那条路要用它
        # （没给则现取 optional 的 approval 服务；两者都没有 → fail-closed）。
        ToolsPlugin(approval=approval),
    ]
    plugins.append(token_usage_plugin())   # `_03`：用量账本 + 用量页面（8767）
    plugins.append(plugin_admin_plugin())  # `_05` 收尾：插件管理（分类 + 一键开关）
    plugins.append(context_meter_plugin(capacity=context_window))  # `_04`：仪表（8768）
    plugins.append(compact_plugin(capacity=context_window))        # `_05`：压缩
    plugins += tool_plugins(workspace=workspace, shell=shell == "local")  # `_06`：工具插件
    if self_tools:
        # 装了自扩展工具就一并装浏览器审批：装卸/安装类操作要有人点头，
        # 没有终端可问时由界面弹卡片（无界面时该策略 fail-closed）。
        plugins.append(web_approval_plugin())
        plugins.append(self_tools_plugin())
    plugins.append(AgentPlugin(approval=approval, max_steps=max_steps, token_budget=token_budget))
    return boot(plugins=plugins, workspace=workspace, roles=roles)


__all__ = [
    "INTERFACE_ROLES",
    "Booted",
    "boot",
    "boot_offline",
    "boot_realtime",
]
