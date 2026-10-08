"""_03_Prompt_Trace 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 是本阶段的交互式入口：--dump-prompt 打印装配单（组成 / 来源 / 变量 /
重建校验），会话内 /prompt 同款。这里不真的开交互终端，而是把它拆成可测的三段：

1. `show_trace` 的打印：来源、引用变量、重建校验行（"✓ 重建一致"）；
2. `latest_trace_with_source`：从日志里取"当前生效装配单"及其位置；
3. `--ask --dump-prompt` 端到端：跑一个 turn，打印装配单并校验重建。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试。

运行：cd P2_Coding/_03_Prompt_Trace && python -m pytest -q
"""

from __future__ import annotations

from chat import build_assembler, latest_trace_with_source, main, show_trace

from context import collect_runtime_context, open_context_harness
from harness.llm import FakeLLM, text_reply
from harness.session import JsonlStore
from prompt import capture_trace

VARS = {
    "cwd": "D:/ws",
    "platform": "DemoOS",
    "time": "2026-10-08T09:00:00+08:00",
}


def test_build_assembler_base_and_active():
    """基础装配器 4 节；叠加 chat 作用域后 5 节，追加节来源 = chat。"""
    base, active = build_assembler(with_scope=True)
    assert [s.name for s in base.parts()] == ["role", "tools", "env", "style"]
    assert [s.name for s in active.parts()] == ["role", "tools", "env", "style", "conversation"]
    assert active.parts()[-1].source == "chat"


def test_show_trace_prints_sources_and_rebuild_check(capsys):
    """show_trace：打印每节来源 / 引用变量；末尾给出重建校验结论。"""
    _, active = build_assembler(with_scope=True)
    trace = capture_trace(active, VARS)
    ok = show_trace(trace.to_dict(), "测试装配单")

    assert ok is True
    out = capsys.readouterr().out
    for name in ("role", "tools", "env", "style", "conversation"):
        assert name in out
    assert "来源=chat" in out
    assert "引用变量=cwd、platform、time" in out
    assert "✓ 重建一致" in out


def test_show_trace_flags_tampered_record(capsys):
    """被打过手的记录：show_trace 报"重建不一致"（校验不是走过场）。"""
    _, active = build_assembler(with_scope=True)
    data = capture_trace(active, VARS).to_dict()
    data["text"] = data["text"] + "（篡改）"
    ok = show_trace(data, "被篡改的装配单")

    assert ok is False
    assert "✗ 重建不一致" in capsys.readouterr().out


def test_latest_trace_with_source(tmp_path):
    """从日志里取装配单及位置：开场时来自 session/start。"""
    workspace = tmp_path / "ws"
    ctx = open_context_harness(
        "s1",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=workspace,
        context_source=lambda: collect_runtime_context(workspace, platform_name="DemoOS"),
    )
    trace, label = latest_trace_with_source(ctx.session.events)
    assert trace is not None
    assert label.startswith("session/start #")


def test_ask_once_with_dump_prompt(tmp_path, capsys):
    """--ask --dump-prompt 端到端：装配明细 + 回复 + 日志落盘。"""
    root = tmp_path / "sessions"
    workspace = tmp_path / "ws"
    rc = main(
        [
            "--ask", "帮我算 1234*56.78",
            "--fake",
            "--dump-prompt",
            "--session", "ask-dump",
            "--root", str(root),
            "--workspace", str(workspace),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "装配单" in out
    assert "来源=builtin" in out
    assert "✓ 重建一致" in out
    assert "70066.52" in out

    events, _ = JsonlStore(root).load("ask-dump")
    start = next(e for e in events if e.type == "session/start")
    assert "prompt_trace" in start.data  # 装配单确实进了日志


def test_ask_once_without_dump_keeps_log_clean_of_prints(tmp_path, capsys):
    """不带 --dump-prompt 时不打印装配明细，但日志里仍有装配单（记录与展示解耦）。"""
    rc = main(
        [
            "--ask", "帮我算 2+3",
            "--fake",
            "--session", "ask-plain",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "来源=" not in out            # 没有打印装配明细
    assert "✓ 重建一致" not in out
    events, _ = JsonlStore(tmp_path / "sessions").load("ask-plain")
    assert "prompt_trace" in next(e for e in events if e.type == "session/start").data
