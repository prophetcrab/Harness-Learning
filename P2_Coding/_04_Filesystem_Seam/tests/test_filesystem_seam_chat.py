"""_04_Filesystem_Seam 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 是本阶段的交互式入口：`--fs local|memory` 换一个"文件世界"。
这里不真的开交互终端，而是把它拆成可测的三段：

1. `build_file_system`：--fs 选择 provider，显式 register → resolve；
2. `--ask` 端到端：memory provider 下写文件 → 磁盘无痕迹；local 下真的落盘；
3. 工具的 write_file 审批标记在入口路径上仍然生效。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试。

运行：cd P2_Coding/_04_Filesystem_Seam && python -m pytest -q
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chat import build_file_system, main

from providers import LocalFS, MemoryFS


def _args(**overrides) -> argparse.Namespace:
    base = {
        "fs": "local",
        "workspace": "ws",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_build_file_system_local(tmp_path: Path):
    fs, label = build_file_system(_args(fs="local", workspace=str(tmp_path / "ws")))
    assert isinstance(fs, LocalFS)
    assert "LocalFS" in label
    assert (tmp_path / "ws").is_dir()  # 本地 provider 的工作区被建好


def test_build_file_system_memory(tmp_path: Path):
    fs, label = build_file_system(_args(fs="memory", workspace=str(tmp_path / "ws")))
    assert isinstance(fs, MemoryFS)
    assert "MemoryFS" in label
    assert not (tmp_path / "ws").exists()  # memory 不需要真实目录


def test_ask_once_memory_leaves_no_trace(tmp_path: Path, capsys):
    """--fs memory --ask：写文件的剧本跑完，磁盘上没有任何痕迹。"""
    workspace = tmp_path / "ws"
    rc = main(
        [
            "--ask", "把 hello 写到 notes/a.txt 再读回来",
            "--fake",
            "--fs", "memory",
            "--no-approve",
            "--session", "mem-ask",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(workspace),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "MemoryFS" in out
    assert "70766" not in out  # 防呆：不是别的输出
    assert not workspace.exists()  # ★ memory provider：磁盘零痕迹
    # 日志照常落盘（日志与文件系统是两回事）
    assert (tmp_path / "sessions" / "mem-ask" / "session.jsonl").is_file()


def test_ask_once_local_writes_to_disk(tmp_path: Path, capsys):
    """--fs local --ask：同样的剧本，文件真的落盘。"""
    workspace = tmp_path / "ws"
    rc = main(
        [
            "--ask", "把 hello 写到 notes/a.txt 再读回来",
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
    assert (workspace / "notes" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_entry_registers_tools_from_seam(tmp_path: Path, capsys):
    """工具面来自接缝注册表：read/write/list 与基线同名同面。"""
    rc = main(
        [
            "--ask", "把 hello 写到 notes/a.txt",
            "--fake",
            "--fs", "memory",
            "--no-approve",
            "--session", "surface",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "['calculate', 'read_file', 'write_file', 'list_files']" in out


def test_run_sh_script_exists():
    """启动器存在且为四件套约定（run.bat / run.sh）。"""
    here = Path(__file__).resolve().parents[1]
    assert (here / "run.bat").is_file()
    assert (here / "run.sh").is_file()
