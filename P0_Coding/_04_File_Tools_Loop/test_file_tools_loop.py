"""_04_File_Tools_Loop 的验收测试：黑盒跑脚本，验证文件工具与安全约束。

测试分两类：
- 离线测试：--tool-only 模式验证工具与路径防护（不联网、不需要 key）。
- 隔离测试：复制脚本到无 .env 的临时目录，验证无 key 时的明确报错。

注意：测试直接在练习目录里运行（工作区就是 demo_workspace），
fixture 文件用完即删，不对演示文件做任何修改。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FOLDER = Path(__file__).parent
SCRIPT = FOLDER / "file_tools_loop.py"
WORKSPACE = FOLDER / "demo_workspace"


def run_tool_only(*args: str) -> subprocess.CompletedProcess[str]:
    """在练习目录里运行 --tool-only 模式（真实工作区）。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--tool-only", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=FOLDER,
    )


def test_list_files_lists_workspace():
    """list_files 返回工作区文件清单。"""
    result = run_tool_only("list_files")
    assert result.returncode == 0
    assert '"files"' in result.stdout
    assert '"count"' in result.stdout


def test_read_file_returns_content():
    """read_file 能读到文件内容；fixture 文件用完即删。"""
    fixture = WORKSPACE / "_test_fixture.txt"
    try:
        fixture.write_text("测试内容 12345", encoding="utf-8")
        result = run_tool_only("_test_fixture.txt")
        assert result.returncode == 0
        assert "测试内容 12345" in result.stdout
        assert '"chars": 10' in result.stdout
    finally:
        fixture.unlink(missing_ok=True)


def test_missing_file_is_structured_error():
    """读不存在的文件：结构化错误，而不是崩溃。"""
    result = run_tool_only("这个文件不存在.txt")
    assert result.returncode == 0
    assert "文件不存在" in result.stdout


def test_path_escape_is_rejected():
    """--tool-only 内置越界测试：读工作区外路径必须被拒绝（沙箱约束）。"""
    result = run_tool_only("list_files")
    assert result.returncode == 0
    assert "非法路径" in result.stdout


def test_no_key_fails_loud_with_hint():
    """无 key 且非 --tool-only 时：明确报错并提示 --tool-only 出路。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "P0_Coding" / "_04_File_Tools_Loop"
        folder.mkdir(parents=True)
        copied = folder / SCRIPT.name
        shutil.copy(SCRIPT, copied)

        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        result = subprocess.run(
            [sys.executable, str(copied)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=folder,
        )

    assert result.returncode == 1
    assert "未找到 DEEPSEEK_API_KEY" in result.stderr
    assert "--tool-only" in result.stderr
