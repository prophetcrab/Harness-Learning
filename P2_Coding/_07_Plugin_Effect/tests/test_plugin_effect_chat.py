"""_07_Plugin_Effect 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 现在经**插件装载**装配（fs + subprocess + toolbox 三个插件进 ctx），
本文件测试：

1. `build_toolbox_via_plugins`：三个插件进台账、槽位齐全、工具面可用；
2. `--ask` 端到端：插件装配的会话跑完一个 turn；退出时整体卸载无异常；
3. `/unload` 回卷演示（交互命令）与退出时自动回卷的日志输出。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试。

运行：cd P2_Coding/_07_Plugin_Effect && python -m pytest -q
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chat import build_toolbox_via_plugins, main

from providers import (
    FS_CAPABILITY,
    SUBPROCESS_CAPABILITY,
    TOOLBOX_CAPABILITY,
    CommandResult,
    MemoryFS,
    ScriptedSubprocess,
)


def _args(**overrides) -> argparse.Namespace:
    base = {
        "workspace": "ws",
        "search": False,
        "shell_timeout": 15.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_build_toolbox_via_plugins_registers_three_plugins(tmp_path: Path):
    args = _args(workspace=str(tmp_path / "ws"))
    fs, _ = MemoryFS(), None
    shell = ScriptedSubprocess([CommandResult(0, "ok\n")])
    plugin_ctx, registry = build_toolbox_via_plugins(args, fs, shell)

    assert plugin_ctx.effect_names == [
        "plugin:fs:MemoryFS",
        "plugin:subprocess:ScriptedSubprocess",
        "plugin:toolbox",
    ]
    assert plugin_ctx.slots == [FS_CAPABILITY, SUBPROCESS_CAPABILITY, TOOLBOX_CAPABILITY]
    assert "shell" in registry.names


def test_build_toolbox_via_plugins_without_shell(tmp_path: Path):
    """不给 subprocess：只装 fs + toolbox（工具面没有 shell）。"""
    args = _args(workspace=str(tmp_path / "ws"))
    plugin_ctx, registry = build_toolbox_via_plugins(args, MemoryFS(), None)
    assert plugin_ctx.effect_names == ["plugin:fs:MemoryFS", "plugin:toolbox"]
    assert "shell" not in registry.names


def test_ask_once_and_unload_all(tmp_path: Path, capsys):
    """--ask：插件装配的会话跑一个 turn；退出时整体卸载并打印回卷对照。"""
    rc = main(
        [
            "--ask", "用 shell 跑一下",
            "--fake",
            "--shell", "fake",
            "--no-approve",
            "--session", "plugin-ask",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "插件台账" in out                       # 启动横幅显示台账
    assert "整体卸载后 ctx 槽位：[]" in out          # 退出时的回卷演示
    assert "ScriptedSubprocess" in out
    assert (tmp_path / "sessions" / "plugin-ask" / "session.jsonl").is_file()


def test_interactive_unload_command(tmp_path: Path, capsys, monkeypatch):
    """/unload：现场卸载 toolbox 插件、观察槽位回卷、再装回去。"""
    lines = iter(["/unload", "/ctx", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    rc = main(
        [
            "--fake",
            "--shell", "fake",
            "--no-approve",
            "--session", "plugin-unload",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "卸载 toolbox 后" in out
    assert "又回来了" in out
    assert "整体卸载后 ctx 槽位：[]" in out


def test_interactive_ctx_command(tmp_path: Path, capsys, monkeypatch):
    """/ctx：打印插件台账与槽位。"""
    lines = iter(["/ctx", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    rc = main(
        [
            "--fake",
            "--shell", "fake",
            "--no-approve",
            "--session", "plugin-ctx",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "插件台账（装载顺序）" in out
    assert "plugin:fs:" in out
    assert "plugin:toolbox" in out
