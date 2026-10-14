"""P3 插件层测试：服务提供、工具面组装、事件协作、依赖回卷。"""

from __future__ import annotations

from pathlib import Path

import pytest

from components.llm import LLMRequest, LLMResponse, text_reply, tool_call_reply
from components.tools import AutoDeny, ScriptedApprover
from kernel import Context, DuplicateSlotError
from plugins import (
    AGENT_SERVICE,
    FS_SERVICE,
    PROMPT_SERVICE,
    PROMPT_VARIABLES,
    SESSIONS_SERVICE,
    TOOLS_SERVICE,
    AgentPlugin,
    PromptPlugin,
    SessionsPlugin,
    ToolsPlugin,
    boot_offline,
    fake_llm_plugin,
    local_fs_plugin,
    memory_fs_plugin,
)

# ---------------------------------------------------------------------------
# 服务提供与工具面
# ---------------------------------------------------------------------------


def test_fs_plugins_provide_same_slot_and_differ_by_name(tmp_path: Path):
    ctx = Context()
    ctx.load_plugin(memory_fs_plugin())
    assert ctx.require(FS_SERVICE).__class__.__name__ == "MemoryFS"
    assert "fs:MemoryFS" in ctx.active_plugins

    with pytest.raises(DuplicateSlotError):
        ctx.load_plugin(local_fs_plugin(tmp_path))  # 槽位已被占：fail loud

    ctx.unload_plugin("fs:MemoryFS")
    ctx.load_plugin(local_fs_plugin(tmp_path))
    assert ctx.require(FS_SERVICE).__class__.__name__ == "LocalFS"


def test_tools_plugin_registers_full_toolbox_and_rolls_back(tmp_path: Path):
    ctx = Context()
    ctx.load_plugins([memory_fs_plugin(), ToolsPlugin(workspace=tmp_path)])
    registry = ctx.require(TOOLS_SERVICE)
    names = registry.names
    for expected in (
        "calculate",
        "read_file",
        "write_file",
        "list_files",
        "edit_file",
        "search_text",
        "find_files",
    ):
        assert expected in names
    assert "shell" not in names  # 没有 subprocess 能力：shell 不注册

    ctx.unload_plugin("tools")
    assert registry.names == []  # 卸载回卷：注册进外部注册表的工具被注销
    assert ctx.services == [FS_SERVICE]  # fs 还在：tools 只撤自己的东西


def test_shell_tool_appears_only_with_subprocess_capability(tmp_path: Path):

    with_shell = boot_offline(
        root=tmp_path / "s1", workspace=tmp_path / "ws1", script=[text_reply("ok")]
    )
    assert "shell" in with_shell.ctx.require(TOOLS_SERVICE).names
    with_shell.close()


def test_write_file_needs_approval_and_deny_is_structured(tmp_path: Path):
    booted = boot_offline(
        root=tmp_path / "s",
        workspace=tmp_path / "ws",
        approval=AutoDeny("测试拒绝"),
        script=[
            tool_call_reply("write_file", {"path": "a.txt", "content": "x"}),
            text_reply("好的，写入被拒绝。"),
        ],
    )
    result = booted.agent.ask("写个文件", session_id="s")
    assert result.status == "done"
    tool_step = [step for step in result.steps if step.tool_results][0]
    tool_result = tool_step.tool_results[0]
    assert tool_result.error is True
    assert tool_result.result["denied"] is True
    booted.close()


# ---------------------------------------------------------------------------
# 事件协作：prompt/variables 瀑布 / llm 前后广播
# ---------------------------------------------------------------------------


def test_prompt_variables_waterfall_from_external_plugin(tmp_path: Path, ):
    ctx = Context()

    class ClockPlugin:
        name = "clock"

        def setup(self, inner: Context) -> None:
            inner.on(
                PROMPT_VARIABLES,
                # `_04` 起默认提示词引用 {{date}}（一天内不变，缓存友好）；
                # 这个瀑布插件同时注入 date 与 time，验证两者都可用。
                lambda value, **kw: {
                    **value,
                    "date": "2026-10-13",
                    "time": "2026-10-13T00:00:00+08:00",
                },
            )

    ctx.load_plugins(
        [ClockPlugin(), SessionsPlugin(tmp_path / "s"), PromptPlugin(workspace=tmp_path)]
    )
    rendered = ctx.require(PROMPT_SERVICE).render()
    assert "2026-10-13" in rendered        # {{date}} 被注入的取值渲染出来


def test_llm_events_are_broadcast_around_each_call(tmp_path: Path):
    seen: list[str] = []
    ctx = Context()

    class Watcher:
        name = "watcher"

        def setup(self, inner: Context) -> None:
            # llm/request 是瀑布：观察者签名照写、返回 None 表示不改。
            inner.on("llm/request", lambda value, **kw: seen.append("request"))
            inner.on("llm/response", lambda **kw: seen.append("response"))

    ctx.load_plugins(
        [
            Watcher(),
            fake_llm_plugin([text_reply("hi")]),
            memory_fs_plugin(),
            SessionsPlugin(tmp_path / "s"),
            PromptPlugin(workspace=tmp_path),
            ToolsPlugin(workspace=tmp_path),
            AgentPlugin(),
        ]
    )
    ctx.require(AGENT_SERVICE).ask("你好", session_id="s")
    assert seen == ["request", "response"]


def test_llm_request_waterfall_can_rewrite_request(tmp_path: Path):
    class Rewriter:
        name = "rewriter"

        def setup(self, inner: Context) -> None:
            def rewrite(request: LLMRequest, **kw) -> LLMRequest:
                return LLMRequest(messages=request.messages, tools=request.tools, model="pinned")

            inner.on("llm/request", rewrite)

    seen_models: list[str | None] = []

    class Spy:
        name = "spy"

        def complete(self, request: LLMRequest) -> LLMResponse:
            seen_models.append(request.model)
            return text_reply("done")

    from components.env import load_env  # noqa: F401  （仅示意：真实 provider 的入口）

    ctx = Context()
    ctx.load_plugins(
        [
            Rewriter(),
            fake_llm_plugin([text_reply("ok")]),  # 注意：瀑布改的是"请求对象"
            memory_fs_plugin(),
            SessionsPlugin(tmp_path / "s"),
            PromptPlugin(workspace=tmp_path),
            ToolsPlugin(workspace=tmp_path),
            AgentPlugin(),
        ]
    )
    # 换掉 llm 服务为 Spy：观察瀑布改后的请求（先卸后装，走插件生命周期）。
    ctx.unload_plugin("llm")
    from plugins import LlmPlugin

    ctx.load_plugin(LlmPlugin(Spy()))
    ctx.require(AGENT_SERVICE).ask("你好", session_id="s2")
    assert seen_models == ["pinned"]


def test_scripted_approver_used_by_agent(tmp_path: Path):
    from components.tools import ApprovalDecision

    approver = ScriptedApprover([ApprovalDecision(True, "剧本批准")])
    booted = boot_offline(
        root=tmp_path / "s",
        workspace=tmp_path / "ws",
        approval=approver,
        script=[
            tool_call_reply("write_file", {"path": "a.txt", "content": "hi"}),
            text_reply("已写入"),
        ],
    )
    result = booted.agent.ask("写 a.txt", session_id="s")
    assert result.final_text == "已写入"
    approver.assert_all_consumed()
    booted.close()


# ---------------------------------------------------------------------------
# sessions 服务：打开即恢复 + 分叉
# ---------------------------------------------------------------------------


def test_sessions_open_is_create_and_resume_same_path(tmp_path: Path):
    ctx = Context()
    ctx.load_plugin(SessionsPlugin(tmp_path / "s"))
    service = ctx.require(SESSIONS_SERVICE)

    session, resumed, report = service.open("s1", system_prompt="你好")
    assert resumed is False and report.repaired is False
    session.append("user/message", {"content": "第一句"})

    again, resumed2, _ = service.open("s1", system_prompt="你好")
    assert resumed2 is True
    assert again.last_seq == session.last_seq
    assert [e.type for e in again.events] == ["session/start", "user/message"]


def test_sessions_fork_copies_prefix_and_refuses_overwrite(tmp_path: Path):
    ctx = Context()
    ctx.load_plugin(SessionsPlugin(tmp_path / "s"))
    service = ctx.require(SESSIONS_SERVICE)
    session, _, _ = service.open("a")
    session.append("user/message", {"content": "1"})
    session.append("user/message", {"content": "2"})

    forked = service.fork("a", "b", upto_seq=2)
    assert [e.type for e in forked.events] == ["session/start", "user/message"]
    assert [e.type for e in session.events] == [
        "session/start",
        "user/message",
        "user/message",
    ]  # 原日志不动

    with pytest.raises(FileExistsError):
        service.fork("a", "b")
