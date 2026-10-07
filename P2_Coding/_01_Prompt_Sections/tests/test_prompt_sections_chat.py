"""_01_Prompt_Sections 的入口集成测试：`chat.py`（离线，非交互）。

`chat.py` 是本阶段的交互式入口（真实对话）。这里不真的开交互终端，而是把它拆成
可测的两段：
1. `build_assembler()` 装配出的系统提示词；
2. 把这段提示词交给 `MiniHarness.open(system_prompt=...)` 后，**确实进了会话日志**。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试（避免联网/耗额度）。

运行：cd P2_Coding/_01_Prompt_Sections && python -m pytest -q
"""

from __future__ import annotations

from chat import build_assembler, show_prompt

from harness.llm import FakeLLM, text_reply
from harness.mini import MiniHarness
from harness.session import JsonlStore


def test_build_assembler_base_and_active():
    """基础装配器 3 节；叠加 chat 作用域后 4 节，且追加的 conversation 节来源为 chat。"""
    base, active = build_assembler(with_scope=True)
    assert [s.name for s in base.parts()] == ["role", "tools", "style"]
    assert [s.name for s in active.parts()] == ["role", "tools", "style", "conversation"]

    # 追加节的来源被标为作用域名
    assert active.parts()[-1].source == "chat"
    # 基础层未被改动（作用域只追加，没有遮蔽）
    assert [s.name for s in base.parts()] == ["role", "tools", "style"]
    assert "多轮对话" in active.assemble()
    assert "多轮对话" not in base.assemble()


def test_build_assembler_without_scope():
    """--no-scope：基础与生效是同一份（3 节，没有 conversation）。"""
    base, active = build_assembler(with_scope=False)
    assert active is base
    assert "多轮对话" not in active.assemble()


def test_chat_prompt_lands_in_session_log(tmp_path):
    """把 chat.py 的装配结果喂给 MiniHarness.open，装配文本进 session/start。"""
    _, active = build_assembler(with_scope=True)
    harness, _ = MiniHarness.open(
        "chat-test",
        provider=FakeLLM([text_reply("你好")]),
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        system_prompt=active.assemble(),
    )

    assert harness.messages[0].role == "system"
    assert harness.messages[0].content == active.assemble()

    events, _ = JsonlStore(tmp_path / "sessions").load("chat-test")
    start = next(e for e in events if e.type == "session/start")
    assert start.data["system_prompt"] == active.assemble()


def test_show_prompt_renders_sections(capsys):
    """show_prompt 打印装配明细：出现 section 名与来源。"""
    _, active = build_assembler(with_scope=True)
    show_prompt(active, "测试")
    out = capsys.readouterr().out
    for name in ("role", "tools", "style", "conversation"):
        assert name in out
    assert "来源=chat" in out
