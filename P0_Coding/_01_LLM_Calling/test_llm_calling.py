"""_01_LLM_Calling 的验收测试：把脚本当黑盒跑，不联网、不需要 key。

脚本是平铺的单文件（没有可 import 的函数），所以测试用 subprocess 运行它：
把脚本复制到一个没有 .env 的临时目录、并清掉环境变量里的 key，
脚本就会在打印完装配结果后以"缺少 key"退出——正好用来验收装配逻辑。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("llm_calling.py")


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """在隔离环境下运行脚本：无 .env、无 DEEPSEEK_API_KEY。"""
    with tempfile.TemporaryDirectory() as tmp:
        # 复制成 <tmp>/P0_Coding/_01_LLM_Calling/llm_calling.py，
        # 脚本推导出的 PROJECT_ROOT 即为 <tmp>，其中没有 .env。
        folder = Path(tmp) / "P0_Coding" / "_01_LLM_Calling"
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


def test_assembly_printed_and_fails_loud_without_key():
    """无 key 时：打印完整装配结果，不残留 {{变量}}，并明确报错退出。"""
    result = run_script()
    assert result.returncode == 1
    assert "未找到 DEEPSEEK_API_KEY" in result.stderr

    out = result.stdout
    assert "{{" not in out  # 所有变量都已解析
    # 小节按注册顺序出现
    assert out.index("[角色设定]") < out.index("[工作环境]") < out.index("[输出要求]")


def test_question_from_command_line():
    """命令行传入的问题出现在输出里（装配发生在调用之前）。"""
    result = run_script("这是一个自定义的测试问题")
    assert "这是一个自定义的测试问题" in result.stdout
