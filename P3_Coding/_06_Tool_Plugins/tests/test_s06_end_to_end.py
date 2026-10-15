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
        "token-usage",          # `_03`：用量账本 + 用量页面
        "plugin-admin",         # `_05` 收尾：插件管理（分类 + 一键开关）
        "context-meter",        # `_04`：上下文仪表
        "compact",              # `_05`：上下文压缩（服务名 compacter）
        # `_06`：工具插件（工具内容从 tools 底座里拆出来，各自独立装卸）
        "tool-calc",
        "tool-fs",
        "tool-search",
        "tool-shell",
        "agent",
    ]
    assert booted.ctx.services == [
        "llm", "fs", "subprocess", "sessions", "prompt", "tools",
        "tool-admin",           # `_06`：工具面管理（注册表由 tools 提供）
        "tools.surface",        # `_06`：工具管理的管理页自述 + 两个端点
        "token-usage",
        "plugin-admin", "plugin-admin.surface",   # `_05` 收尾：插件管理 + 端点
        "context-meter", "compacter",
        "compact.surface",      # `_05`：压缩插件的界面自述（显示规则 + 端点）
        "agent",
    ]
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
# 观察者与多轮（根因级回归）
# ---------------------------------------------------------------------------


def test_observer_is_resolved_per_turn_not_captured_at_open(tmp_path: Path):
    """观察者是"每轮现取"：换观察者后，事件必须落在**当前**观察者上。

    根因级回归（"只聊一轮就死"）：sink 曾在会话首次打开时捕获观察者，
    第二轮起事件被推给第一轮留下的旧观察者（现实中是已关闭的 HTTP 连接），
    写死套接字抛 OSError 打断整轮。这里在两个 turn 之间更换观察者并断言归属。
    """
    booted = _offline(tmp_path, [text_reply("第一"), text_reply("第二"), text_reply("第三")])
    first_calls: list[str] = []
    second_calls: list[str] = []

    booted.agent.open("s1", on_event=lambda kind, payload: first_calls.append(kind))
    booted.agent.ask("第一问", session_id="s1")
    assert "turn_start" in first_calls and "turn_end" in first_calls

    booted.agent.open("s1", on_event=lambda kind, payload: second_calls.append(kind))
    booted.agent.ask("第二问", session_id="s1")

    # 第二轮的每个事件都给了新观察者；旧观察者从换人那一刻起不再增长。
    assert "turn_start" in second_calls and "turn_end" in second_calls
    assert first_calls.count("turn_end") == 1  # 只有第一轮那一次
    assert second_calls.count("turn_end") == 1

    booted.agent.ask("第三问", session_id="s1")
    assert second_calls.count("turn_end") == 2  # 第三轮仍在"当前"观察者
    assert first_calls.count("turn_end") == 1  # 旧观察者没有再收到
    booted.close()


# ---------------------------------------------------------------------------
# 热插拔与回卷
# ---------------------------------------------------------------------------


def test_swapping_fs_plugin_deactivates_and_reactivates_dependents(tmp_path: Path):
    booted = _offline(tmp_path, [text_reply("ok")])
    assert "tools" in booted.ctx.active_plugins

    booted.ctx.unload_plugin("fs:MemoryFS")
    # `_06` 的变化：**退位的只有真正依赖 fs 的那些**——tool-fs / tool-search
    # 要文件系统读写与检索，所以它们随提供者退位；而 tools 底座只是 optional
    # 依赖 fs（"装新工具插件"那条路才读源码），工具面与 agent 照常活着。
    #
    # 对比 `_05`：那时工具内容焊在 tools 里，tools 不得不 inject fs，于是
    # 换个文件系统就把整个工具面（连同 list_tools 这类管理工具）一起拆掉了。
    assert set(booted.ctx.pending_plugins) == {"tool-fs", "tool-search"}
    assert TOOLS_SERVICE in booted.ctx.services
    registry = booted.ctx.require(TOOLS_SERVICE)
    assert {"calculate", "list_tools"} <= set(registry.names)   # 不依赖 fs 的工具照常在
    assert "read_file" not in registry.names

    disk_ws = tmp_path / "disk_ws"
    booted.ctx.load_plugin(local_fs_plugin(disk_ws))
    assert FS_SERVICE in booted.ctx.services
    assert set(booted.ctx.active_plugins) >= {"tools", "agent", "fs:LocalFS"}
    assert "read_file" in registry.names                         # fs 回来，工具自动回位

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


def test_default_is_unlimited_steps(tmp_path: Path):
    """默认不限步数：长程任务不会被截断（回归：曾默认 8 步）。

    用剧本让模型连调 12 次工具再收尾——若仍有默认上限，turn 会以
    `max_steps` 结束；现在应一路跑完到 `done`。
    """
    calls = [
        tool_call_reply("calculate", {"expression": f"{i}+{i}"}) for i in range(1, 13)
    ]
    booted = _offline(tmp_path, calls + [text_reply("十二步都算完了。")])
    result = booted.agent.ask("连算十二次", session_id="long")
    assert result.status == "done"
    assert len(result.steps) == 13          # 12 次工具 + 1 次收尾
    assert result.final_text == "十二步都算完了。"
    booted.close()


def test_explicit_max_steps_still_caps(tmp_path: Path):
    """显式设限时仍能触顶（能力保留，只是不再是默认）。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[tool_call_reply("calculate", {"expression": "1+1"})] * 5,
        max_steps=3,
    )
    result = booted.agent.ask("会触顶", session_id="capped")
    assert result.status == "max_steps" and len(result.steps) == 3
    booted.close()
