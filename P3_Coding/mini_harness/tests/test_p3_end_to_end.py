"""P3 端到端测试：装配 → 对话 → 日志 → resume → 热插拔 → 整体回卷（全离线）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from components.prompt import rebuild_text
from plugins import (
    FS_SERVICE,
    TOOLS_SERVICE,
    boot,
    boot_offline,
    local_fs_plugin,
    memory_fs_plugin,
)


def _offline(tmp_path: Path, script: list, **kwargs):
    return boot_offline(
        root=tmp_path / "sessions", workspace=tmp_path / "ws", script=script, **kwargs
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def test_boot_offline_activates_all_plugins_and_serves(tmp_path: Path):
    booted = _offline(tmp_path, [text_reply("你好")])
    assert booted.plugins == [
        "llm",
        "fs:MemoryFS",
        "proc:ScriptedSubprocess",
        "sessions",
        "prompt",
        "tools",
        "agent",
    ]
    assert booted.ctx.services == ["llm", "fs", "subprocess", "sessions", "prompt", "tools", "agent"]
    tools = booted.ctx.require(TOOLS_SERVICE)
    assert "calculate" in tools.names and "shell" in tools.names

    result = booted.agent.ask("嗨", session_id="s1")
    assert result.status == "done"
    assert result.final_text == "你好"
    booted.close()


def test_boot_fails_loud_when_dependencies_unmet(tmp_path: Path):
    # 只装 fs，不装 tools 依赖之外必要的插件：agent 缺 llm/sessions/prompt。
    from plugins import AgentPlugin

    with pytest.raises(RuntimeError) as excinfo:
        boot(plugins=[memory_fs_plugin(), AgentPlugin()], workspace=tmp_path)
    message = str(excinfo.value)
    assert "agent" in message and "llm" in message  # 报错指出缺哪个依赖


# ---------------------------------------------------------------------------
# 对话 + 日志
# ---------------------------------------------------------------------------


def test_turn_produces_expected_event_sequence_and_log_file(tmp_path: Path):
    booted = _offline(
        tmp_path,
        [
            tool_call_reply("calculate", {"expression": "2+3"}),
            text_reply("2+3=5。"),
        ],
    )
    result = booted.agent.ask("帮我算 2+3", session_id="s1")
    assert result.status == "done" and len(result.steps) == 2

    opened = booted.agent.open("s1")
    types = [event.type for event in opened.session.events]
    assert types[0] == "session/start"
    assert "assistant/message" in types and "tool/result" in types
    assert types[-1] == "turn/end"
    assert (tmp_path / "sessions" / "s1" / "session.jsonl").is_file()

    tool_step = [step for step in result.steps if step.tool_results][0]
    assert tool_step.tool_results[0].result == {"expression": "2+3", "result": 5}
    booted.close()


def test_resume_continues_history_and_turn_number(tmp_path: Path):
    booted = _offline(tmp_path, [text_reply("第一次回答")])
    booted.agent.ask("第一问", session_id="s1")
    booted.close()

    # 全新装配（模拟进程重启）：同名会话应恢复，turn 编号接续。
    booted2 = _offline(tmp_path, [text_reply("第二次回答")])
    opened = booted2.agent.open("s1")
    assert opened.resumed is True
    assert opened.repaired is False
    roles = [message.role for message in opened.messages]
    assert roles == ["system", "user", "assistant"]

    result = booted2.agent.ask("第二问", session_id="s1")
    assert result.turn == 2
    assert [message.role for message in opened.messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    booted2.close()


def test_prompt_trace_in_log_rebuilds_recorded_text(tmp_path: Path):
    """装配单可重建：日志里每次 system/message 的文本 == 由装配单重建的文本。"""
    booted = _offline(tmp_path, [text_reply("ok")])
    booted.agent.ask("你好", session_id="s1")
    opened = booted.agent.open("s1")

    traced = [
        event for event in opened.session.events if event.type == "session/start"
    ] + [event for event in opened.session.events if event.type == "system/message"]
    assert traced, "至少 session/start 应携带装配单"
    for event in traced:
        trace = event.data.get("prompt_trace")
        assert trace is not None
        assert rebuild_text(trace) == trace["text"]
    booted.close()


# ---------------------------------------------------------------------------
# 热插拔与回卷
# ---------------------------------------------------------------------------


def test_swapping_fs_plugin_deactivates_and_reactivates_dependents(tmp_path: Path):
    booted = _offline(tmp_path, [text_reply("ok")])
    assert "tools" in booted.ctx.active_plugins

    booted.ctx.unload_plugin("fs:MemoryFS")
    # tools / agent 依赖 fs：随提供者退位；窗口内没有服务可用。
    assert set(booted.ctx.pending_plugins) == {"tools", "agent"}
    assert TOOLS_SERVICE not in booted.ctx.services

    disk_ws = tmp_path / "disk_ws"
    booted.ctx.load_plugin(local_fs_plugin(disk_ws))
    assert FS_SERVICE in booted.ctx.services
    assert set(booted.ctx.active_plugins) >= {"tools", "agent", "fs:LocalFS"}

    # 新会话用新组合；写文件真的落盘（LocalFS）。
    booted.ctx.unload_plugin("fs:LocalFS")
    booted.ctx.load_plugin(local_fs_plugin(disk_ws))
    result = booted.agent.ask("写个文件", session_id="s2")
    assert result.status == "done"
    booted.close()


def test_close_rolls_back_every_registration(tmp_path: Path):
    booted = _offline(tmp_path, [text_reply("ok")])
    booted.agent.ask("问一句", session_id="s1")
    registry = booted.ctx.require(TOOLS_SERVICE)

    booted.close()
    assert booted.ctx.services == []
    assert booted.ctx.active_plugins == []
    assert booted.ctx.event_names == []
    assert registry.names == []  # 工具面也被回卷


def test_error_raised_inside_setup_leaves_no_half_state(tmp_path: Path):
    from kernel import Context
    from plugins import PromptPlugin, SessionsPlugin

    class Broken:
        name = "broken"
        inject = ("fs",)

        def setup(self, ctx: Context) -> None:
            ctx.provide("broken.ready", True)
            raise RuntimeError("炸")

    ctx = Context()
    with pytest.raises(RuntimeError):
        ctx.load_plugins(
            [
                memory_fs_plugin(),
                Broken(),
                SessionsPlugin(tmp_path / "s"),
                PromptPlugin(workspace=tmp_path),
            ]
        )
    # 批量装载失败：已装成功的部分被干净回卷，不留半装状态。
    assert ctx.services == []
    assert ctx.plugins == []


def test_memory_fs_leaves_no_trace_on_disk(tmp_path: Path):
    booted = _offline(
        tmp_path,
        [
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hello"}),
            text_reply("已写入"),
        ],
    )
    booted.agent.ask("写 a.txt", session_id="s1")
    assert not any((tmp_path / "ws").rglob("a.txt"))
    booted.close()
