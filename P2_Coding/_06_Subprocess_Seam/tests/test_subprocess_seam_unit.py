"""_06_Subprocess_Seam 的单元测试：命令接缝的契约与两个实现。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（providers/subprocess*.py），以及它们与工具层的契约。

覆盖：
1. 契约：两个实现都满足 SubprocessService；返回同一种 CommandResult
2. LocalSubprocess：stdout / stderr / 退出码分开捕获；cwd 生效；超时是结果；
   启动失败是异常；环境剥除（凭证不进子进程，其余保留）
3. ScriptedSubprocess：剧本队列 / 请求记录 / 用尽报错 / assert_all_consumed
4. 工具层渲染：正常 / 非零 / 超时三种形状；输出截断；cwd 与期限来自组装

运行：cd P2_Coding/_06_Subprocess_Seam && python -m pytest -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from providers import (
    SHELL_NONZERO_EXIT,
    SHELL_SPAWN_FAILED,
    SHELL_TIMEOUT,
    CommandResult,
    LocalSubprocess,
    MemoryFS,
    ScriptedSubprocess,
    SubprocessError,
    SubprocessService,
    build_toolbox,
)

PYTHON = shutil.which("python") or sys.executable


def py(code: str) -> str:
    """跨平台的 python -c 调用（带引号处理，供测试构造命令用）。"""
    return f'"{PYTHON}" -c "{code}"'


# =========================================================================
# 1) 契约：两个实现同形
# =========================================================================


def test_both_implementations_satisfy_protocol():
    assert isinstance(LocalSubprocess(), SubprocessService)
    assert isinstance(ScriptedSubprocess([]), SubprocessService)


def test_both_return_command_result():
    real = LocalSubprocess().run(py("print(1)"))
    fake = ScriptedSubprocess([CommandResult(0, "1\n")]).run("whatever")
    assert isinstance(real, CommandResult) and isinstance(fake, CommandResult)


# =========================================================================
# 2) LocalSubprocess：真跑
# =========================================================================


def test_stdout_and_exit_code():
    result = LocalSubprocess().run(py("print(6*7)"))
    assert result.exit_code == 0
    assert result.stdout.strip() == "42"
    assert result.stderr == ""
    assert result.timed_out is False


def test_stderr_captured_separately():
    """stdout 与 stderr 分开——模型要能分辨"输出"与"报错"。"""
    result = LocalSubprocess().run(
        py("import sys; print('out'); sys.stderr.write('err'); sys.exit(3)")
    )
    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_nonzero_exit_is_a_result_not_exception():
    result = LocalSubprocess().run(py("import sys; sys.exit(9)"))
    assert result.exit_code == 9  # 不抛异常


def test_cwd_is_honored(tmp_path: Path):
    result = LocalSubprocess().run(py("import os; print(os.getcwd())"), cwd=str(tmp_path))
    assert result.stdout.strip().lower() == str(tmp_path).lower()


def test_timeout_is_a_result_with_partial_output():
    """超时被杀是"结果"：timed_out=True、无退出码、已刷出的输出带回。

    注意 flush=True：进程被硬杀（整树 SIGKILL/taskkill），还留在缓冲区里的
    输出必然丢失——这是"杀"的固有语义，不是接缝的选择。
    """
    result = LocalSubprocess().run(
        py("import time; print('before', flush=True); time.sleep(5)"), timeout=0.6
    )
    assert result.timed_out is True
    assert result.exit_code is None
    assert "before" in result.stdout  # 已被刷出的输出仍能拿到


def test_timeout_kills_the_whole_tree_promptly():
    """超时真正终结命令本体：设 0.6 秒的期限，整次调用应在 1 秒级收场。

    （回归用例：曾经只杀 shell，命令本体（孙进程）继续跑并占着输出管道，
    实测 0.6 秒的超时要 30 秒才返回。）
    """
    import time

    started = time.monotonic()
    result = LocalSubprocess().run(
        py("import time; time.sleep(30)"), timeout=0.6
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 5  # 远小于命令本体的 30 秒


def test_spawn_failure_is_loud(tmp_path: Path):
    """工作目录不存在 → 基础设施失败，抛 SubprocessError（而非结果）。"""
    with pytest.raises(SubprocessError) as info:
        LocalSubprocess().run(py("print(1)"), cwd=str(tmp_path / "no-such-dir"))
    assert info.value.code == SHELL_SPAWN_FAILED


def test_blocked_env_is_scrubbed_but_rest_kept(monkeypatch):
    monkeypatch.setenv("SEAM_SECRET_MARKER", "topsecret")
    script = (
        "import os; "
        "print(os.environ.get('SEAM_SECRET_MARKER', 'ABSENT'), "
        "os.environ.get('SEAM_KEEP_MARKER', 'ABSENT'))"
    )
    monkeypatch.setenv("SEAM_KEEP_MARKER", "kept")

    scrubbed = LocalSubprocess(blocked_env=("SEAM_SECRET_MARKER",)).run(py(script))
    assert scrubbed.stdout.split() == ["ABSENT", "kept"]  # 凭证被剥、其余保留

    default = LocalSubprocess().run(py(script))
    assert default.stdout.split() == ["topsecret", "kept"]  # 默认黑名单不含它


def test_default_blocklist_contains_api_key():
    assert "DEEPSEEK_API_KEY" in LocalSubprocess().blocked_env


# =========================================================================
# 3) ScriptedSubprocess：剧本纪律
# =========================================================================


def test_scripted_replays_in_order_and_records_requests():
    sp = ScriptedSubprocess([CommandResult(0, "a"), CommandResult(1, "b")])
    assert sp.run("first", cwd="w", timeout=3).stdout == "a"
    assert sp.run("second").stdout == "b"
    assert [r.command for r in sp.requests] == ["first", "second"]
    assert sp.requests[0].cwd == "w" and sp.requests[0].timeout == 3
    sp.assert_all_consumed()


def test_scripted_exhaustion_is_loud():
    sp = ScriptedSubprocess([CommandResult(0, "only")])
    sp.run("first")
    with pytest.raises(AssertionError, match="剧本已用完"):
        sp.run("second")


def test_scripted_leftover_is_loud():
    sp = ScriptedSubprocess([CommandResult(0, "x"), CommandResult(0, "y")])
    sp.run("first")
    with pytest.raises(AssertionError, match="还剩 1 条"):
        sp.assert_all_consumed()


def test_scripted_runs_no_real_process():
    """剧本 provider 不碰世界：描述一个不可能的命令也照常回放。"""
    sp = ScriptedSubprocess([CommandResult(0, "pretend")])
    assert sp.run("this-command-does-not-exist --nope").stdout == "pretend"


# =========================================================================
# 4) 工具层渲染（消费者契约）
# =========================================================================


def _shell_of(scripted: ScriptedSubprocess, **toolbox_kwargs):
    registry = build_toolbox(MemoryFS(), scripted, workspace="/demo/ws", **toolbox_kwargs)
    return registry.resolve("shell").func


def test_shell_renders_success():
    sp = ScriptedSubprocess([CommandResult(0, "ok\n")])
    result = _shell_of(sp)(command="echo ok")
    assert result == {"command": "echo ok", "exit_code": 0, "stdout": "ok\n", "stderr": ""}
    sp.assert_all_consumed()


def test_shell_renders_nonzero_as_error():
    sp = ScriptedSubprocess([CommandResult(2, "some out", "bad things\n")])
    result = _shell_of(sp)(command="false")
    assert result["code"] == SHELL_NONZERO_EXIT
    assert result["exit_code"] == 2
    assert result["stdout"] == "some out" and result["stderr"] == "bad things\n"
    sp.assert_all_consumed()


def test_shell_renders_timeout_as_error_with_partial_output():
    sp = ScriptedSubprocess([CommandResult(None, "partial", "", timed_out=True)])
    result = _shell_of(sp, shell_timeout=3)(command="sleep")
    assert result["code"] == SHELL_TIMEOUT
    assert result["exit_code"] is None
    assert result["stdout"] == "partial"
    sp.assert_all_consumed()


def test_shell_renders_spawn_failure_as_error(tmp_path: Path):
    """基础设施失败（provider 抛异常）也被翻成结构化结果，不炸循环。"""
    service = LocalSubprocess()
    registry = build_toolbox(
        MemoryFS(), service, workspace=str(tmp_path / "no-such-dir")
    )
    result = registry.resolve("shell").func(command="echo x")
    assert result["code"] == SHELL_SPAWN_FAILED


def test_shell_truncates_long_output():
    sp = ScriptedSubprocess([CommandResult(0, "x" * 10_000)])
    result = _shell_of(sp, shell_max_output_chars=100)(command="big")
    assert len(result["stdout"]) < 200
    assert "截断" in result["stdout"] and "10000" in result["stdout"]
    sp.assert_all_consumed()


def test_shell_cwd_and_timeout_come_from_assembly():
    """模型只给 command；cwd 与期限在组装时解析（模型不可改）。"""
    sp = ScriptedSubprocess([CommandResult(0, "")])
    _shell_of(sp, shell_timeout=7)(command="whoami")
    request = sp.requests[0]
    assert request.timeout == 7
    assert request.cwd == str(Path("/demo/ws"))
    sp.assert_all_consumed()


def test_shell_needs_approval():
    registry = build_toolbox(
        MemoryFS(), ScriptedSubprocess([]), workspace="/demo/ws"
    )
    assert registry.resolve("shell").definition.needs_approval is True


def test_shell_absent_without_subprocess_service():
    """不给 SubprocessService 就不注册 shell——工具面随能力装配。"""
    registry = build_toolbox(MemoryFS())
    assert "shell" not in registry.names


def test_local_subprocess_smoke_with_real_shell():
    """LocalSubprocess 经工具层的一次真实冒烟（用 python 保证跨平台）。"""
    registry = build_toolbox(MemoryFS(), LocalSubprocess(), workspace=".")
    result = registry.resolve("shell").func(command=py("print(2+2)"))
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "4"


def test_subprocess_env_is_clean_by_default(monkeypatch):
    """经接缝跑出的子进程默认拿不到项目凭证（与 mock 无关的真实验证）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    registry = build_toolbox(MemoryFS(), LocalSubprocess(), workspace=".")
    result = registry.resolve("shell").func(
        command=py("import os; print(os.environ.get('DEEPSEEK_API_KEY', 'ABSENT'))")
    )
    assert result["stdout"].strip() == "ABSENT"


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 上做一次 cmd 冒烟")
def test_windows_shell_smoke():
    result = subprocess.run(
        'echo hello', shell=True, capture_output=True, text=True, encoding="utf-8"
    )
    assert "hello" in result.stdout
