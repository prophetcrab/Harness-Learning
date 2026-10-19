"""project —— 项目（基础插件）：一个 AI 服务的装配面。

必要依赖（插件级）：`workspace`、`sessions`、`session-store-jsonl`、
`agent-admin`（它再拉入 agent / agent-loop）。

**它做什么**：一个项目 = "在哪干活" + "跟谁聊" + "记在哪" + "谁来干"：

    project.workspace     当前工作区
    project.sessions      会话内核
    project.store         持久化
    project.agents        agent 管理（模式 / 创建 / 停止）
    project.start(...)    按模式开一个 agent（**唯一入口**）
    project.info()        一眼状态（工作区 / 会话 / 存储 / 在跑的 agent）

**为什么 `start()` 只走 agent-admin**（实测教训）：会话的 `session/start`
只能写一次。如果先 `sessions.open()` 再建 agent，agent-loop 渲染好的系统
提示词与装配单就写不进去了——日志里留下一条空提示词的 `session/start`。
所以入口必须**唯一**：由 agent-loop 负责"渲染提示词 + 建会话"，
project 只做转发。

**为什么是必需依赖**：没有工作区就无处干活，没有会话就无法对话，没有持久化
就丢历史，没有 agent 就无法干活——缺一个，"AI 服务"这个概念就不成立。所以用
`requires`（插件级依赖）在装配期 fail loud，而不是运行到一半才发现。

**它不新增能力**：不自己读写文件、不自己存日志、不自己跑循环——只把服务
绑在一起，并提供"项目视角"的入口与状态。
"""

from __future__ import annotations

from typing import Any

from core import Context

# --- 服务/事件名（字符串，本地复刻）---
AGENT_ADMIN_SERVICE = "agent-admin"
SESSIONS_SERVICE = "sessions"
SESSION_STORE_SERVICE = "session-store-jsonl"
WORKSPACE_SERVICE = "workspace"

#: 项目服务的名字。
PROJECT_SERVICE = "project"


class Project:
    """一个项目的四个面：工作区 / 会话 / 存储 / agent。"""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    @property
    def workspace(self):
        return self._ctx.require(WORKSPACE_SERVICE)

    @property
    def sessions(self):
        return self._ctx.require(SESSIONS_SERVICE)

    @property
    def store(self):
        return self._ctx.require(SESSION_STORE_SERVICE)

    @property
    def agents(self):
        return self._ctx.require(AGENT_ADMIN_SERVICE)

    # ------------------------------------------------------------------
    # 启动一个 agent（唯一入口）
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        mode: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        **options: Any,
    ) -> Any:
        """按模式开一个 agent；返回句柄（followup / steer / cancel / dispose）。

        `session_id` 的三种情况：

            内存里已有        → resume（复用已打开的历史）
            磁盘上有历史      → resume（从 JSONL 读回）
            都没有 / 没给 id  → create（新建一条）

        **不先调 `sessions.open()`**：会话由 agent-loop 在 create/resume 时
        建立并写入渲染好的提示词——两个入口会导致 `session/start` 写两次
        （第一次是空的，提示词就丢了）。

        `**options` 覆盖模式里的同名字段（provider / model / max_steps …）。
        """
        if session_id is not None and (
            self.sessions.get(session_id) is not None or self._has_persisted(session_id)
        ):
            return self.agents.resume(
                session_id, mode=mode, agent_id=agent_id, **options
            )
        return self.agents.create(
            mode=mode, session_id=session_id, agent_id=agent_id, **options

        )
    def _has_persisted(self, session_id: str) -> bool:
        try:
            return bool(self.store.load(session_id))
        except Exception:  # noqa: BLE001 —— 读不到就当没有
            return False

    def stop(self, agent_id: str) -> bool:
        """停掉一个 agent。"""
        return self.agents.stop(agent_id)

    def options_of(self, mode: str) -> Any:
        """看某个模式最终会用什么配置（不创建 agent）。"""
        return self.agents.describe(mode).options()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """项目状态：工作区 / 会话 / 存储 / 在跑的 agent。"""
        return {
            "workspace": self.workspace.info(),
            "sessions": list(self.sessions.session_ids),
            "store": self.store.snapshot(),
            "agents": self.agents.snapshot()["agents"],
        }


class ProjectPlugin:
    """提供 `project` 服务。装配单里应声明 `role="base"`。"""

    name = "project"
    #: 插件级依赖：四样缺一不可（见模块文档）。
    requires = ("workspace", "sessions", "session-store-jsonl", "agent-admin")

    def setup(self, ctx: Context) -> None:
        ctx.provide(PROJECT_SERVICE, Project(ctx))


def project_plugin(**config: Any) -> ProjectPlugin:
    """工厂（当前无配置——参数都在它依赖的插件里）。"""
    return ProjectPlugin(**config)


__all__ = [
    "PROJECT_SERVICE",
    "Project",
    "ProjectPlugin",
    "project_plugin",
]
