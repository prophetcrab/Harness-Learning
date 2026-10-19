"""最小应用装配单：一个能跑真 turn 的 AI 服务。

这是 `manifests/minimal.py` 的数据来源——同一份单子同时用于：
    · 测试（装配 + 跑一个 turn）
    · 命令行（`python app.py --manifest minimal`）

**为什么是 Python 数据而不是 YAML**（0042 的待定项）：现在是第一个消费者，
用 Python 字面量零依赖、能被测试直接 import、写错字段在装配期就报。等 M4
出现三个产品形态、需要"改配置不改代码"时再定文件格式。

**它包含什么**：基础插件（工作区 / 会话 / 持久化 / LLM / 提示词 / 工具面 /
agent 三件套）+ 六件工具 + 两层文件策略 + 命令后端 + project。
**唯一的模型后端是 `llm-fake`**——离线可跑，接真实模型时把 `llm-provider`
那一行换掉即可（装配单是数据，不用改代码）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from plugins.base.agent import agent_plugin
from plugins.base.fs import fs_plugin
from plugins.base.llm import llm_plugin
from plugins.base.proc import proc_plugin
from plugins.base.project import project_plugin
from plugins.base.prompt import prompt_plugin
from plugins.base.sessions import sessions_plugin
from plugins.base.tools_admin import tools_admin_plugin
from plugins.base.workspace import workspace_plugin
from plugins.capability.agent.admin import agent_admin_plugin
from plugins.capability.agent.loop import agent_loop_plugin
from plugins.capability.approval.terminal import approval_terminal_plugin
from plugins.capability.fs.observation_policy import fs_observation_policy_plugin
from plugins.capability.fs.sandbox import fs_sandbox_plugin
from plugins.capability.llm.fake import fake_llm_plugin
from plugins.capability.llm.provider import llm_provider_plugin
from plugins.capability.proc.backends import (
    proc_git_bash_plugin,
    proc_posix_plugin,
    proc_scripted_plugin,
)
from plugins.capability.session.store_jsonl import session_store_jsonl_plugin
from plugins.capability.tools.ask_user import tool_ask_user_plugin
from plugins.capability.tools.fs import tool_fs_plugin
from plugins.capability.tools.fs_search import tool_fs_search_plugin
from plugins.capability.tools.math import tool_math_plugin
from plugins.capability.tools.shell import tool_shell_plugin
from plugins.capability.tools.web_search import tool_web_search_plugin


def catalog() -> dict[str, Any]:
    """名字 → 工厂（装配单里 `name` 字段解析到这里）。"""
    return {
        # 基础
        "workspace": workspace_plugin,
        "fs": fs_plugin,
        "proc-service": proc_plugin,
        "sessions": sessions_plugin,
        "llm": llm_plugin,
        "prompt": prompt_plugin,
        "tools-admin": tools_admin_plugin,
        "agent": agent_plugin,
        "agent-loop": agent_loop_plugin,
        "agent-admin": agent_admin_plugin,
        "project": project_plugin,
        # 能力：持久化 / 策略 / 后端
        "session-store-jsonl": session_store_jsonl_plugin,
        "fs-sandbox": fs_sandbox_plugin,
        "approval-terminal": approval_terminal_plugin,
        "fs-observation-policy": fs_observation_policy_plugin,
        "proc-local-git-bash": proc_git_bash_plugin,
        "proc-local-posix": proc_posix_plugin,
        "proc-scripted": proc_scripted_plugin,
        # 能力：工具
        "tool-math": tool_math_plugin,
        "tool-fs": tool_fs_plugin,
        "tool-shell": tool_shell_plugin,
        "tool-fs-search": tool_fs_search_plugin,
        "tool-web-search": tool_web_search_plugin,
        "tool-ask-user": tool_ask_user_plugin,
        # 模型后端：真实模型与离线剧本共用这一个适配器，只换 transport
        "llm-provider": llm_provider_plugin,
        # 纯剧本适配器（YAML 装配单里没法写 transport 函数，用这类插件离线跑）
        "llm-fake": fake_llm_plugin,
    }


def _offline_transport(replies: list[str]):
    """离线传输：不联网，按顺序吐剧本回复（循环使用）。

    这是 `llm-provider` 的**传输替身**——适配器本身是真的（方言、参数、
    校验全走真实路径），只是网络那一层换成了本地剧本。接真实模型时把这一行
    换成真实 `base_url` 即可。
    """
    state = {"index": 0}

    def transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float):
        text = replies[state["index"] % len(replies)]
        state["index"] += 1
        import json

        events = [
            json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False),
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": sum(
                            len(m.get("content") or "") for m in payload.get("messages", [])
                        )
                        // 4,
                        "completion_tokens": len(text) // 4,
                    },
                },
                ensure_ascii=False,
            ),
        ]
        return [f"data: {event}" for event in events] + ["data: [DONE]"]

    return transport


def manifest(
    *,
    workspace: str | Path = "workspace",
    sessions: str | Path = "sessions",
    sandbox_mode: str = "changes",
    replies: list[str] | None = None,
    approval_policy: str = "ask",
) -> list[dict[str, Any]]:
    """最小可运行装配单（离线）。

    参数是**部署决策**（目录 / 沙箱模式 / 剧本），不是插件逻辑。
    """
    script = replies or ["（离线模型）你好。"]
    return [
        {
            "id": "app",
            "children": [
                # ---------- 基础 ----------
                {"id": "workspace", "name": "workspace", "role": "base",
                 "config": {"root": str(workspace)}},
                {"id": "fs", "name": "fs", "role": "base"},
                {"id": "proc", "name": "proc-service", "role": "base"},
                {"id": "sessions", "name": "sessions", "role": "base"},
                {"id": "store", "name": "session-store-jsonl", "role": "capability",
                 "config": {"root": str(sessions)}},
                {"id": "prompt", "name": "prompt", "role": "base",
                 "config": {"sections": [
                     {"name": "identity", "order": 10,
                      "text": "你是一个严谨的编程助手。先读再改，改动尽量小而完整。"},
                     {"name": "cwd", "order": 30, "text": "工作目录：{{cwd}}"},
                 ]}},
                {"id": "llm", "name": "llm", "role": "base",
                 "config": {"default_provider": "fake", "default_model": "fake-1"}},
                {"id": "llm-provider", "name": "llm-provider", "role": "capability",
                 "config": {
                     "route": "fake",
                     "base_url": "https://offline.invalid/chat",
                     "api_key": "offline",
                     "models": [{"id": "fake-1", "context_window": 128000,
                                 "max_output_tokens": 4096}],
                     "transport": _offline_transport(script),
                 }},
                # ---------- 工具面 ----------
                {"id": "tools", "name": "tools-admin", "role": "base"},
                {"id": "math", "name": "tool-math", "role": "capability"},
                {"id": "tool-fs", "name": "tool-fs", "role": "capability"},
                {"id": "tool-shell", "name": "tool-shell", "role": "capability"},
                {"id": "tool-search", "name": "tool-fs-search", "role": "capability"},
                {"id": "tool-web", "name": "tool-web-search", "role": "capability"},
                {"id": "tool-ask", "name": "tool-ask-user", "role": "capability"},
                # ---------- 策略 / 后端 ----------
                {"id": "approval", "name": "approval-terminal", "role": "capability",
                 "config": {"policy": approval_policy}},
                {"id": "sandbox", "name": "fs-sandbox", "role": "capability",
                 "config": {"mode": sandbox_mode}},
                {"id": "observation", "name": "fs-observation-policy", "role": "capability"},
                {"id": "backend-bash", "name": "proc-local-git-bash", "role": "capability"},
                {"id": "backend-posix", "name": "proc-local-posix", "role": "capability"},
                {"id": "backend-scripted", "name": "proc-scripted", "role": "capability"},
                # ---------- agent + 应用入口 ----------
                {"id": "agent", "name": "agent", "role": "base"},
                {"id": "loop", "name": "agent-loop", "role": "capability"},
                {"id": "admin", "name": "agent-admin", "role": "capability"},
                {"id": "project", "name": "project", "role": "base"},
            ],
        }
    ]


__all__ = ["catalog", "manifest"]
