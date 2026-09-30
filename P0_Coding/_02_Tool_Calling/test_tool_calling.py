"""_02_Tool_Calling 的验收测试：黑盒跑脚本，验证工具调用的关键行为。

测试分两类：
- 离线测试：不需要 key、不联网，用隔离环境跑脚本。
- 联网测试：--search-only 模式访问真实 Bing；断网时自动跳过。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FOLDER = Path(__file__).parent


def run_script(*args: str, script_name: str = "tool_calling.py") -> subprocess.CompletedProcess[str]:
    """在隔离环境中运行指定脚本：复制到临时目录（那里没有 .env），并清掉 key。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "P0_Coding" / "_02_Tool_Calling"
        folder.mkdir(parents=True)
        copied = folder / script_name
        shutil.copy(FOLDER / script_name, copied)

        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        return subprocess.run(
            [sys.executable, str(copied), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )


def test_no_key_fails_loud_with_hint():
    """没有 key 时：打印工具清单和问题，然后明确报错，并提示 --search-only。"""
    result = run_script("随便问点什么")
    assert result.returncode == 1
    assert "未找到 DEEPSEEK_API_KEY" in result.stderr
    assert "web_search" in result.stdout  # 工具说明书已展示
    assert "--search-only" in result.stderr  # 报错里给出了离线验证的出路


def test_search_only_needs_no_key():
    """--search-only 不需要 key，直接返回搜索结果（需要网络，断网则跳过）。"""
    result = run_script("--search-only", "Python 编程语言")
    if result.returncode != 0 and ("URLError" in result.stderr or "timed out" in result.stderr):
        import pytest

        pytest.skip("当前网络无法访问 Bing，跳过联网用例")

    assert result.returncode == 0, result.stderr
    assert "共" in result.stdout and "条" in result.stdout  # 打印了条数
    assert "http" in result.stdout  # 至少有一条真实链接


def test_inspect_script_fails_loud_without_key():
    """观察脚本同样在缺少 key 时明确报错（它的调用检查在进入循环之前）。"""
    result = run_script(script_name="inspect_model_calls.py")
    assert result.returncode == 1
    assert "未找到 DEEPSEEK_API_KEY" in result.stderr
