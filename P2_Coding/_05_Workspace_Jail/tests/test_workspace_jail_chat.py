"""_05_Workspace_Jail 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 现在是三选一的对话入口：`--fs local|jail|memory`。它的离线剧本
**先试一次越界写**（`../escape.txt`），因此一次 `--ask` 就能演示本阶段机制：

- `--fs jail`：越界写被结构化拒绝（详情在会话日志的 tool/result 里）、磁盘无痕迹；
- `--fs local`：同样的请求直接放行——文件出现在工作区外（对照组）；
- 交互模式下第二幕会写一个区内文件，验证围栏只拦越界、不碍正常操作。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试。

运行：cd P2_Coding/_05_Workspace_Jail && python -m pytest -q
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chat import build_file_system, main

from harness.session import JsonlStore
from providers import FS_SANDBOX_DENIED, LocalFS, MemoryFS, WorkspaceJailFS


def _args(**overrides) -> argparse.Namespace:
    base = {"fs": "local", "workspace": "ws"}
    base.update(overrides)
    return argparse.Namespace(**base)


def _tool_results(root: Path, session: str) -> list[dict]:
    events, _ = JsonlStore(root).load(session)
    return [e.data for e in events if e.type == "tool/result"]


def test_build_file_system_jail(tmp_path: Path):
    fs, label = build_file_system(_args(fs="jail", workspace=str(tmp_path / "ws")))
    assert isinstance(fs, WorkspaceJailFS)
    assert isinstance(fs.base, LocalFS)  # 围栏叠在真实磁盘上
    assert "WorkspaceJailFS" in label
    assert (tmp_path / "ws").is_dir()  # 工作区被建好


def test_build_file_system_local_and_memory(tmp_path: Path):
    local_fs, _ = build_file_system(_args(fs="local", workspace=str(tmp_path / "ws")))
    memory_fs, _ = build_file_system(_args(fs="memory", workspace=str(tmp_path / "ws")))
    assert isinstance(local_fs, LocalFS)
    assert isinstance(memory_fs, MemoryFS)


def test_ask_once_jail_blocks_escape(tmp_path: Path, capsys):
    """--fs jail：剧本第一幕越界写 → 结构化拒绝；工作区外的文件不存在。"""
    workspace = tmp_path / "ws"
    rc = main(
        [
            "--ask", "把这行字写到 ../escape.txt",
            "--fake",
            "--fs", "jail",
            "--no-approve",
            "--session", "jail-ask",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(workspace),
        ]
    )
    assert rc == 0
    assert "WorkspaceJailFS" in capsys.readouterr().out

    results = _tool_results(tmp_path / "sessions", "jail-ask")
    assert results and results[0]["is_error"] is True
    assert results[0]["result"]["code"] == FS_SANDBOX_DENIED
    assert not (tmp_path / "escape.txt").exists()  # ★ 越界写没有发生


def test_ask_once_local_allows_escape(tmp_path: Path, capsys):
    """对照组：--fs local 的同样剧本——越界写成功，文件出现在工作区外。"""
    workspace = tmp_path / "ws"
    rc = main(
        [
            "--ask", "把这行字写到 ../escape.txt",
            "--fake",
            "--fs", "local",
            "--no-approve",
            "--session", "local-ask",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(workspace),
        ]
    )
    assert rc == 0
    assert "LocalFS" in capsys.readouterr().out

    assert (tmp_path / "escape.txt").read_text(encoding="utf-8") == "越狱尝试"
    results = _tool_results(tmp_path / "sessions", "local-ask")
    assert results[0]["is_error"] is False
    # → jail 与 local 的唯一变量就是那层围栏


def test_interactive_jail_second_turn_writes_inside(tmp_path: Path, capsys, monkeypatch):
    """交互模式：第一幕越界被拒后，第二幕的区内写照常成功（围栏不碍正常操作）。"""
    workspace = tmp_path / "ws"
    lines = iter(["把 x 写到 ../escape.txt", "再写一个区内的", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    rc = main(
        [
            "--fake",
            "--fs", "jail",
            "--no-approve",
            "--session", "jail-interactive",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(workspace),
        ]
    )
    assert rc == 0
    assert (workspace / "notes" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "escape.txt").exists()

    results = _tool_results(tmp_path / "sessions", "jail-interactive")
    codes = [item["result"].get("code") for item in results]
    assert FS_SANDBOX_DENIED in codes  # 越界那条
    assert any(item["is_error"] is False for item in results)  # 区内那条成功
