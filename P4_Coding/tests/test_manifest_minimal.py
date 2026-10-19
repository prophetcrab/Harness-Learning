"""最小应用装配验收：launcher + 装配单 → 能跑真 turn 的服务。

这条测试就是 `python app.py --ask ...` 的程序化版本——两者走同一份装配单。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import PolicyError, boot
from manifests.minimal import catalog, manifest
from plugins.base.project import PROJECT_SERVICE
from plugins.capability.agent.admin import AGENT_ADMIN_SERVICE


def start(tmp_path: Path, *, replies: list[str] | None = None):
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")
    runtime = boot(
        manifest(
            workspace=workspace,
            sessions=sessions,
            sandbox_mode="changes",
            replies=replies or ["离线回答"],
        ),
        catalog=catalog(),
    )
    return runtime, workspace, sessions


def test_manifest_assembles_all_entries(tmp_path):
    runtime, _ws, _sessions = start(tmp_path)
    try:
        states = runtime.snapshot()
        assert states, "装配单不应为空"
        bad = [state for state in states if state.status not in ("active", "group")]
        assert bad == [], f"有条目没起来：{[(s.path, s.status, s.error) for s in bad]}"
        assert runtime.ctx.require(PROJECT_SERVICE) is not None
        assert set(runtime.ctx.require(AGENT_ADMIN_SERVICE).modes()) >= {
            "default",
            "quick",
            "careful",
        }
    finally:
        runtime.close()


def test_one_real_turn_through_the_manifest(tmp_path):
    runtime, _ws, sessions = start(tmp_path, replies=["回答一"])
    try:
        project = runtime.ctx.require(PROJECT_SERVICE)
        handle = project.start(session_id="s1")
        result = handle.followup("你好")
        assert result.status == "done"
        assert result.final_text == "回答一"
        logs = list(sessions.rglob("session.v1.jsonl"))
        assert logs, "会话日志没有落盘"
        assert logs[0].parent.name == "s1"
        # session/start 里有渲染好的提示词（不是空串）
        from plugins.base.sessions import SESSIONS_SERVICE

        events = runtime.ctx.require(SESSIONS_SERVICE).events("s1")
        assert events[0].kind == "session/start"
        assert events[0].payload["system_prompt"]
    finally:
        runtime.close()


def test_session_survives_restart(tmp_path):
    runtime, ws, sessions = start(tmp_path, replies=["第一次"])
    try:
        runtime.ctx.require(PROJECT_SERVICE).start(session_id="s1").followup("你好")
    finally:
        runtime.close()

    runtime2 = boot(
        manifest(workspace=ws, sessions=sessions, replies=["第二次"]),
        catalog=catalog(),
    )
    try:
        handle = runtime2.ctx.require(PROJECT_SERVICE).start(session_id="s1")
        result = handle.followup("继续")
        assert result.status == "done"
        from plugins.base.sessions import SESSIONS_SERVICE

        messages = runtime2.ctx.require(SESSIONS_SERVICE).messages("s1")
        assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]
    finally:
        runtime2.close()


def test_model_can_call_a_tool(tmp_path):
    """模型发起工具调用 → 工具执行 → 回灌 → 最终回答。"""
    import json

    calls = {"n": 0}

    def transport(url, headers, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            events = [
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c1",
                                            "function": {
                                                "name": "calc",
                                                "arguments": '{"expression":"2+3"}',
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ),
                json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            ]
        else:
            events = [
                json.dumps({"choices": [{"delta": {"content": "等于 5"}}]}),
                json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            ]
        return [f"data: {event}" for event in events] + ["data: [DONE]"]

    workspace = tmp_path / "ws"
    workspace.mkdir()
    entries = manifest(workspace=workspace, sessions=tmp_path / "sessions")
    # 把离线剧本传输替换成"先调工具"的传输
    for entry in entries[0]["children"]:
        if entry["id"] == "llm-provider":
            entry["config"]["transport"] = transport

    runtime = boot(entries, catalog=catalog())
    try:
        result = runtime.ctx.require(PROJECT_SERVICE).start(session_id="s1").followup("2+3")
        assert result.status == "done"
        assert result.final_text == "等于 5"
        assert result.steps == 2
    finally:
        runtime.close()


def test_product_policy_rejects_manifest_without_application(tmp_path):
    """这份单子还没有应用化插件：按产品启动会被政策拒绝（M4 补 cli 后再开）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(PolicyError, match="应用化插件"):
        boot(
            manifest(workspace=workspace, sessions=tmp_path / "sessions"),
            catalog=catalog(),
            product=True,
        )


def test_agent_split_is_interface_vs_implementation(tmp_path):
    """接缝在 base，实现是 capability：与 llm / llm-provider 同构。"""
    from plugins.base.agent import AgentPlugin
    from plugins.base.llm import LLMPlugin
    from plugins.capability.agent.admin import AgentAdminPlugin
    from plugins.capability.agent.loop import AgentLoopPlugin

    # 接缝：base，并声明"必须有一个实现在场"
    assert AgentPlugin.requires == ("agent-loop",)
    assert LLMPlugin.requires == ("llm-provider",)

    # 实现：capability（插件身份没变，只是目录与角色变了）
    assert AgentLoopPlugin.name == "agent-loop"
    assert AgentAdminPlugin.name == "agent-admin"

    # 装配单里的角色
    entries = {
        entry["name"]: entry["role"]
        for entry in manifest(workspace=tmp_path, sessions=tmp_path)[0]["children"]
    }
    assert entries["agent"] == "base"
    assert entries["agent-loop"] == "capability"
    assert entries["agent-admin"] == "capability"
    assert entries["llm"] == "base"
    assert entries["llm-provider"] == "capability"


def test_driver_is_replaceable_in_the_manifest(tmp_path):
    """把 agent-loop 从装配单里去掉 → 装配期就失败（agent 的 requires）。"""
    from core import BootError, boot

    entries = manifest(workspace=tmp_path, sessions=tmp_path)
    children = entries[0]["children"]
    entries[0]["children"] = [e for e in children if e["name"] != "agent-loop"]
    with pytest.raises(BootError, match="agent-loop"):
        boot(entries, catalog=catalog())
