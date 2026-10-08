"""_02_Prompt_Context 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 是本阶段的交互式入口（每 step 渲染 + 真实对话）。这里不真的开交互终端，
而是把它拆成可测的三段：
1. `build_assembler()` 装配出的系统提示词（基础四节 / 叠加 chat 作用域五节）；
2. `show_prompt()` 的打印：模板里保留 {{name}}，渲染结果里是当下取值；
3. `--ask` 单发模式端到端跑通（FakeLLM 剧本，事件落盘）。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试。

运行：cd P2_Coding/_02_Prompt_Context && python -m pytest -q
"""

from __future__ import annotations

from chat import build_assembler, main, show_prompt

from harness.llm import FakeLLM, text_reply
from harness.session import JsonlStore


def test_build_assembler_base_and_active():
    """基础装配器 4 节（role/tools/env/style）；叠加 chat 作用域后 5 节，追加节来源为 chat。"""
    base, active = build_assembler(with_scope=True)
    assert [s.name for s in base.parts()] == ["role", "tools", "env", "style"]
    assert [s.name for s in active.parts()] == ["role", "tools", "env", "style", "conversation"]
    assert active.parts()[-1].source == "chat"
    assert [s.name for s in base.parts()] == ["role", "tools", "env", "style"]  # 基础层未被改动


def test_build_assembler_without_scope():
    """--no-scope：基础与生效是同一份（4 节，没有 conversation）。"""
    base, active = build_assembler(with_scope=False)
    assert active is base
    assert "多轮对话" not in active.assemble(
        {"cwd": "/w", "platform": "p", "time": "t"}
    )


def test_show_prompt_keeps_template_and_renders_variables(capsys):
    """打印明细：模板行保留 {{cwd}} 原文；渲染结果里是当下取值。"""
    _, active = build_assembler(with_scope=True)
    show_prompt(
        active, "测试", {"cwd": "D:/ws", "platform": "DemoOS", "time": "2026-10-08T09:00:00+08:00"}
    )
    out = capsys.readouterr().out
    for name in ("role", "tools", "env", "style", "conversation"):
        assert name in out
    assert "{{cwd}}" in out          # 模板行
    assert "D:/ws" in out            # 渲染结果
    assert "来源=chat" in out


def test_chat_ask_once_end_to_end(tmp_path, capsys):
    """--ask 一条问题跑一个 turn：装配明细打印 + 剧本回复 + 日志落盘。"""
    root = tmp_path / "sessions"
    workspace = tmp_path / "ws"
    rc = main(
        [
            "--ask", "帮我算 1234*56.78",
            "--fake",
            "--session", "chat-ask",
            "--root", str(root),
            "--workspace", str(workspace),
            "--no-approve",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "系统提示词由 section 装配" in out
    assert "70066.52" in out

    events, _ = JsonlStore(root).load("chat-ask")
    assert any(e.type == "session/start" for e in events)
    assert any(e.type == "tool/result" for e in events)


def test_chat_assembled_prompt_lands_in_log(tmp_path):
    """chat 的装配结果被渲染进 session/start（含三个变量在"那一刻"的取值）。"""
    _, active = build_assembler(with_scope=True)
    from context import collect_runtime_context, open_context_harness

    workspace = tmp_path / "ws"
    ctx = open_context_harness(
        "chat-log",
        provider=FakeLLM([text_reply("你好")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        assembler=active,
        context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
    )
    events, _ = JsonlStore(tmp_path / "sessions").load("chat-log")
    start = next(e for e in events if e.type == "session/start")
    assert "多轮对话" in start.data["system_prompt"]   # chat 作用域追加的一节
    assert "DemoOS" in start.data["system_prompt"]
    assert "{{" not in start.data["system_prompt"]     # 占位符已被渲染
    assert ctx.harness.messages[0].content == start.data["system_prompt"]
