"""workspace / workspace-admin / project 验收。

重点：
    · 工作区边界（相对 / 绝对 / `..` / 符号链接）；
    · 管理面：登记 / 切换 / 查看大小（**不删目录**）；
    · project 的三个必需依赖在装配期检查。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import Context
from plugins.base.agent import agent_plugin
from plugins.base.llm import llm_plugin
from plugins.base.loader import AssemblyError, loader_plugin
from plugins.base.project import PROJECT_SERVICE, project_plugin
from plugins.base.prompt import prompt_plugin
from plugins.base.sessions import sessions_plugin
from plugins.base.tools_admin import tools_admin_plugin
from plugins.base.workspace import (
    WORKSPACE_SERVICE,
    Workspace,
    WorkspaceError,
    workspace_plugin,
)
from plugins.capability.agent.admin import agent_admin_plugin
from plugins.capability.agent.loop import agent_loop_plugin
from plugins.capability.llm.fake import fake_llm_plugin
from plugins.capability.llm.provider import llm_provider_plugin
from plugins.capability.session.store_jsonl import session_store_jsonl_plugin
from plugins.capability.workspace.admin import (
    WORKSPACE_ADMIN_SERVICE,
    workspace_admin_plugin,
)

# ---------------------------------------------------------------------------
# 工作区边界
# ---------------------------------------------------------------------------


def make_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_text("hello", encoding="utf-8")
    return Workspace(root)


def test_relative_path_resolves_inside(tmp_path):
    workspace = make_workspace(tmp_path)
    resolved = workspace.resolve("src/a.txt")
    assert resolved == (workspace.root / "src" / "a.txt").resolve()
    assert workspace.contains(resolved)


def test_absolute_path_inside_is_allowed(tmp_path):
    workspace = make_workspace(tmp_path)
    absolute = workspace.root / "src" / "a.txt"
    assert workspace.resolve(absolute) == absolute.resolve()


def test_dotdot_escape_is_rejected(tmp_path):
    workspace = make_workspace(tmp_path)
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="越界"):
        workspace.resolve("../outside.txt")
    with pytest.raises(WorkspaceError, match="越界"):
        workspace.resolve("src/../../outside.txt")


def test_absolute_outside_is_rejected(tmp_path):
    workspace = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="越界"):
        workspace.resolve(tmp_path / "elsewhere.txt")


def test_relative_name_hides_absolute_path(tmp_path):
    workspace = make_workspace(tmp_path)
    assert workspace.relative("src/a.txt") == "src/a.txt"


def test_info_reports_files_and_size(tmp_path):
    workspace = make_workspace(tmp_path)
    info = workspace.info()
    assert info["files"] == 1
    assert info["bytes"] == 5
    assert info["human_size"].endswith("B")
    assert info["root"] == str(workspace.root)


def test_missing_root_fails_loud(tmp_path):
    with pytest.raises(WorkspaceError, match="不是目录|不存在"):
        Workspace(tmp_path / "nope")


def test_workspace_plugin_provides_service(tmp_path):
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path).root))
    workspace = ctx.require(WORKSPACE_SERVICE)
    assert workspace.resolve("src/a.txt").name == "a.txt"


# ---------------------------------------------------------------------------
# workspace-admin
# ---------------------------------------------------------------------------


def booted_admin(tmp_path: Path) -> tuple[Context, object]:
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path).root))
    ctx.load_plugin(workspace_admin_plugin())
    return ctx, ctx.require(WORKSPACE_ADMIN_SERVICE)


def test_admin_starts_with_one_workspace(tmp_path):
    _ctx, admin = booted_admin(tmp_path)
    assert admin.names() == ("default",)
    assert admin.active_name() == "default"
    assert admin.info()["files"] == 1


def test_admin_can_add_and_switch_workspaces(tmp_path):
    _ctx, admin = booted_admin(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.txt").write_text("bb", encoding="utf-8")

    admin.create("other", other)
    assert admin.names() == ("default", "other")
    admin.set_active("other")
    assert admin.active_name() == "other"
    assert admin.active().root == other.resolve()
    assert admin.info()["files"] == 1


def test_admin_list_shows_sizes_and_active(tmp_path):
    _ctx, admin = booted_admin(tmp_path)
    rows = admin.list()
    assert rows[0]["name"] == "default"
    assert rows[0]["active"] is True
    assert rows[0]["bytes"] == 5


def test_admin_remove_does_not_delete_directory(tmp_path):
    _ctx, admin = booted_admin(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    admin.create("other", other)
    admin.remove("other")
    assert admin.names() == ("default",)
    assert other.exists()  # 目录还在（不删文件是刻意的）


def test_admin_cannot_remove_active_or_create_duplicate(tmp_path):
    _ctx, admin = booted_admin(tmp_path)
    with pytest.raises(ValueError, match="当前工作区"):
        admin.remove("default")
    with pytest.raises(ValueError, match="已存在"):
        admin.create("default", tmp_path)
    with pytest.raises(ValueError, match="不存在"):
        admin.create("x", tmp_path / "nope")
    with pytest.raises(ValueError, match="不存在"):
        admin.set_active("nope")


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


def booted_project(tmp_path: Path) -> Context:
    """一个完整的最小项目：工作区 + 会话 + 持久化 + agent 全家桶。"""
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=make_workspace(tmp_path).root))
    ctx.load_plugin(sessions_plugin())
    ctx.load_plugin(session_store_jsonl_plugin(root=tmp_path / "sessions"))
    ctx.load_plugin(llm_plugin(default_provider="fake", default_model="fake-1"))
    ctx.load_plugin(fake_llm_plugin(replies=["ok"]))
    ctx.load_plugin(prompt_plugin())
    ctx.load_plugin(tools_admin_plugin())
    ctx.load_plugin(agent_plugin())
    ctx.load_plugin(agent_loop_plugin())
    ctx.load_plugin(agent_admin_plugin())
    ctx.load_plugin(project_plugin())
    return ctx


def test_project_binds_four_services(tmp_path):
    project = booted_project(tmp_path).require(PROJECT_SERVICE)
    assert project.workspace.root.name == "ws"
    assert project.sessions.session_ids == ()
    assert "sessions" in project.store.snapshot()
    assert project.agents.default_mode == "default"


def test_project_start_creates_agent_and_records_prompt(tmp_path):
    """start() 只走 agent-admin：session/start 由 agent-loop 写，带提示词。"""
    project = booted_project(tmp_path).require(PROJECT_SERVICE)
    handle = project.start(session_id="s1")
    assert handle.session_id == "s1"
    assert handle.status() == "idle"

    start = project.sessions.events("s1")[0]
    assert start.kind == "session/start"
    assert start.payload["model"]["mode"] == "default"

    result = handle.followup("你好")
    assert result.status == "done"
    assert project.sessions.messages("s1")[0].content == "你好"


def test_project_start_resumes_existing_session(tmp_path):
    project = booted_project(tmp_path).require(PROJECT_SERVICE)
    first = project.start(session_id="s1")
    first.followup("第一句")
    first.dispose()

    second = project.start(session_id="s1")
    second.followup("第二句")
    messages = project.sessions.messages("s1")
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]


def test_project_info_aggregates(tmp_path):
    project = booted_project(tmp_path).require(PROJECT_SERVICE)
    project.start(session_id="s1")
    info = project.info()
    assert info["workspace"]["files"] >= 1
    assert info["sessions"] == ["s1"]
    assert info["store"]["root"] == str(tmp_path / "sessions")
    assert info["agents"][0]["session_id"] == "s1"


def test_project_requires_all_dependencies(tmp_path):
    """装配期检查：缺任一必需插件 → AssemblyError。"""
    ctx = Context()
    ctx.load_plugin(
        loader_plugin(
            {
                "workspace": workspace_plugin,
                "sessions": sessions_plugin,
                "session-store-jsonl": session_store_jsonl_plugin,
                "llm": llm_plugin,
                "llm-provider": llm_provider_plugin,
                "prompt": prompt_plugin,
                "tools-admin": tools_admin_plugin,
                "agent": agent_plugin,
                "agent-loop": agent_loop_plugin,
                "agent-admin": agent_admin_plugin,
                "project": project_plugin,
            }
        )
    )
    loader = ctx.require("loader")

    with pytest.raises(AssemblyError, match="插件级依赖"):
        loader.assemble(
            [
                {"id": "ws", "name": "workspace", "config": {"root": str(make_workspace(tmp_path).root)}},
                {"id": "sessions", "name": "sessions"},
                {"id": "project", "name": "project"},  # 缺 session-store-jsonl
            ]
        )
    assert loader.snapshot() == ()


# 完整装配的验收在 tests/test_manifest_minimal.py（用真实装配单）——
# 这里不再手写一份短装配单，避免两处漂移。
