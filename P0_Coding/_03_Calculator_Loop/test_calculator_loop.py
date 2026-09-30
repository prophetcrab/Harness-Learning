"""_03_Calculator_Loop 的验收测试：黑盒跑脚本，验证计算器工具与循环的关键行为。

测试分两类：
- 离线测试：--calc-only 模式验证计算器（不联网、不需要 key），以及无 key 时的明确报错。
- 联网测试：完整循环需要模型 API，这里只在有 key 且网络可用时执行（默认跳过）。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FOLDER = Path(__file__).parent
SCRIPT = FOLDER / "calculator_loop.py"


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """在隔离环境中运行脚本：复制到临时目录（那里没有 .env），并清掉 key。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "P0_Coding" / "_03_Calculator_Loop"
        folder.mkdir(parents=True)
        copied = folder / SCRIPT.name
        shutil.copy(SCRIPT, copied)

        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        return subprocess.run(
            [sys.executable, str(copied), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )


def test_calc_only_computes():
    """--calc-only 走 execute_tool 相同的路径，结果精确。"""
    result = run_script("--calc-only", "1234*56.78")
    assert result.returncode == 0
    assert "70066.52" in result.stdout


def test_calc_only_division_by_zero_is_structured_error():
    """除零不崩溃，而是转成结构化错误（错误也是给模型的输入）。"""
    result = run_script("--calc-only", "1/0")
    assert result.returncode == 0
    assert "失败" in result.stdout
    assert "division by zero" in result.stdout


def test_calc_only_rejects_dangerous_syntax():
    """模型生成的字符串绝不能直接 eval：函数调用等语法必须被白名单拒绝。"""
    result = run_script("--calc-only", "__import__('os').system('dir')")
    assert result.returncode == 0
    assert "失败" in result.stdout
    assert "不支持的语法" in result.stdout


def test_calc_only_rejects_huge_exponent():
    """指数上限防止 9**9**9 这类计算拖垮进程。"""
    result = run_script("--calc-only", "9**9**9")
    assert result.returncode == 0
    assert "指数超出上限" in result.stdout


def test_no_key_fails_loud_with_hint():
    """没有 key 时：明确报错，并提示 --calc-only 出路。"""
    result = run_script()
    assert result.returncode == 1
    assert "未找到 DEEPSEEK_API_KEY" in result.stderr
    assert "--calc-only" in result.stderr
