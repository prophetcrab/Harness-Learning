"""_06_Subprocess_Seam 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 现在两个接缝都可由 CLI 选择：`--fs local|jail|memory`、
`--shell local|fake|auto`。本文件测试：

1. `build_subprocess_service`：--shell local 装配 LocalSubprocess、fake 装配
   ScriptedSubprocess、auto 跟随 --fake（离线永远不真跑命令）；
2. `--ask` 端到端：shell=fake 下模型请求的命令走剧本、零真实进程；
3. 工具面横幅：shell 工具出现在工具列表里。

真实 API 对话属于手动验收（README 的"运行方法"里给了命令），不进测试。

运行：cd P2_Coding/_06_Subprocess_Seam && python -m pytest -q
"""

from __future__ import annotations

import argparse
from pathlib import Path

from chat import build_subprocess_service, main

from harness.session import JsonlStore
from providers import LocalSubprocess, ScriptedSubprocess


def _args(**overrides) -> argparse.Namespace:
    base = {"shell": "auto", "fake": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_build_subprocess_service_local():
    service, label = build_subprocess_service(_args(shell="local", fake=False))
    assert isinstance(service, LocalSubprocess)
    assert "LocalSubprocess" in label


def test_build_subprocess_service_fake():
    service, label = build_subprocess_service(_args(shell="fake", fake=False))
    assert isinstance(service, ScriptedSubprocess)
    assert "ScriptedSubprocess" in label


def test_build_subprocess_service_auto_follows_fake():
    """auto：--fake 时自动切剧本（离线永远不真跑命令）；否则本地。"""
    offline, _ = build_subprocess_service(_args(shell="auto", fake=True))
    online, _ = build_subprocess_service(_args(shell="auto", fake=False))
    assert isinstance(offline, ScriptedSubprocess)
    assert isinstance(online, LocalSubprocess)


def test_ask_once_with_fake_shell_runs_no_process(tmp_path: Path, capsys):
    """--fake --shell fake：剧本里的命令被回放（打印演示输出），零真实进程。"""
    rc = main(
        [
            "--ask", "用 shell 跑一下",
            "--fake",
            "--shell", "fake",
            "--no-approve",
            "--session", "fake-shell",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "ScriptedSubprocess" in out
    assert "shell" in out  # 工具面里含 shell

    events, _ = JsonlStore(tmp_path / "sessions").load("fake-shell")
    results = [e.data for e in events if e.type == "tool/result"]
    assert results and results[0]["name"] == "shell"
    assert results[0]["result"]["exit_code"] == 0  # 剧本第一条：正常结束


def test_ask_once_with_fake_auto_shell(tmp_path: Path, capsys):
    """--fake（不带 --shell）：auto 让命令走剧本——离线路径不会真执行。"""
    rc = main(
        [
            "--ask", "用 shell 跑一下",
            "--fake",
            "--no-approve",
            "--session", "auto-shell",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    assert "ScriptedSubprocess" in capsys.readouterr().out


def test_ask_once_real_shell(tmp_path: Path, capsys):
    """--shell local（真实执行）：命令真的跑了，输出进了日志。"""
    rc = main(
        [
            "--ask", "用 shell 跑一下",
            "--fake",
            "--shell", "local",
            "--no-approve",
            "--session", "real-shell",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "LocalSubprocess" in out

    events, _ = JsonlStore(tmp_path / "sessions").load("real-shell")
    results = [e.data for e in events if e.type == "tool/result"]
    assert results and results[0]["name"] == "shell"
    assert results[0]["result"]["exit_code"] == 0
    assert results[0]["result"]["command"] == "echo demo"  # 结果里带回执行的命令
    # 离线剧本里请求的就是 "echo demo"——真的被 shell 执行了
    assert "demo" in results[0]["result"]["stdout"]


def test_default_timeout_is_applied(tmp_path: Path, capsys):
    """--shell-timeout 生效：超短期限让一个稍慢的命令变成 SHELL_TIMEOUT。"""
    import shutil
    import sys
    import time

    from context import collect_runtime_context, open_context_harness
    from harness.llm import FakeLLM, text_reply, tool_call_reply
    from harness.tools import AutoApprove
    from providers import SHELL_TIMEOUT, LocalFS, build_toolbox

    python = shutil.which("python") or sys.executable
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    registry = build_toolbox(
        LocalFS(tmp_path / "ws"),
        LocalSubprocess(),
        workspace=tmp_path / "ws",
        shell_timeout=0.5,  # ★ 组装时解析的期限
    )
    inner = FakeLLM(
        [
            tool_call_reply("shell", {"command": f'"{python}" -c "import time; time.sleep(2)"'}),
            text_reply("超时了。"),
        ]
    )
    ctx = open_context_harness(
        "short-timeout",
        provider=inner,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=AutoApprove(),
        context_source=lambda: collect_runtime_context(tmp_path / "ws", platform_name="DemoOS"),
        tool_registry=registry,
    )
    started = time.monotonic()
    result = ctx.harness.send("跑个慢的")
    elapsed = time.monotonic() - started
    inner.assert_all_consumed()

    assert result.steps[0].tool_results[0].result["code"] == SHELL_TIMEOUT
    assert elapsed < 10  # 树杀生效：不会等命令本体自己跑完（2 秒）之外太久
