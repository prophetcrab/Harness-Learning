"""_08_Profile_Layers 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 现在完全由配置驱动：`--profile` 选层、`--patch` 叠加、`--dump-config` 看树。
本文件测试：

1. `--dump-config`：打印最终配置树（不 boot、不产生副作用）；
2. `--ask` 端到端（dev profile，全离线）：配置装配的会话跑完一个 turn；
3. `--patch` 叠加：CLI 补丁进树、并在启动横幅里反映；
4. 配置错误：退出码 2 + 报错信息（不抛栈）。

真实 API 对话属于手动验收（`--profile prod`），不进测试。

运行：cd P2_Coding/_08_Profile_Layers && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

from chat import main


def _ask_args(tmp_path: Path, *extra: str, session: str = "cfg-ask") -> list[str]:
    return [
        "--ask", "帮我算 1234*56.78",
        "--profile", "dev",
        "--no-approve",
        "--session", session,
        "--root", str(tmp_path / "sessions"),
        *extra,
    ]


def test_dump_config_prints_tree(tmp_path: Path, capsys):
    rc = main(["--profile", "dev", "--dump-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "profile = dev" in out
    assert "llm" in out and "llm:fake" in out
    assert "fs" in out and "fs:memory" in out
    assert "toolbox" in out and "shell_timeout = 15" in out


def test_dump_config_dev_vs_prod_differ(tmp_path: Path, capsys):
    main(["--profile", "dev", "--dump-config"])
    dev_out = capsys.readouterr().out
    main(["--profile", "prod", "--dump-config"])
    prod_out = capsys.readouterr().out
    assert "llm:fake" in dev_out and "llm:fake" not in prod_out
    assert "llm:deepseek" in prod_out and "llm:deepseek" not in dev_out


def test_ask_once_on_dev_profile(tmp_path: Path, capsys):
    rc = main(_ask_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "profile：dev" in out
    assert "llm        = llm:fake" in out        # 启动横幅里逐行列出配置
    assert "fs         = fs:memory" in out
    assert "FakeLLM" in out
    assert "70066.52" in out
    assert "整体卸载后 ctx 槽位：[]" in out
    assert (tmp_path / "sessions" / "cfg-ask" / "session.jsonl").is_file()


def test_cli_patch_shows_up_in_banner(tmp_path: Path, capsys):
    cli = tmp_path / "cli.yaml"
    cli.write_text("patch:\n  - id: toolbox\n    config:\n      shell_timeout: 3\n", encoding="utf-8")
    rc = main(_ask_args(tmp_path, "--patch", str(cli), session="cfg-patch"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "（+1 层 CLI 补丁）" in out


def test_unknown_profile_fails_loud_no_traceback(tmp_path: Path, capsys):
    rc = main(["--profile", "ghost", "--dump-config"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "[配置错误]" in out
    assert "不存在" in out


def test_unknown_row_id_in_cli_patch_fails_loud(tmp_path: Path, capsys):
    cli = tmp_path / "bad.yaml"
    cli.write_text("patch:\n  - id: nope\n    disabled: true\n", encoding="utf-8")
    rc = main(["--profile", "dev", "--patch", str(cli), "--dump-config"])
    assert rc == 2
    assert "[配置错误]" in capsys.readouterr().out


def test_disable_subprocess_row_via_cli_patch(tmp_path: Path, capsys):
    cli = tmp_path / "cli.yaml"
    cli.write_text("patch:\n  - id: subprocess\n    disabled: true\n", encoding="utf-8")
    rc = main(_ask_args(tmp_path, "--patch", str(cli), session="cfg-disabled"))
    assert rc == 0
    out = capsys.readouterr().out
    # 工具面里没有 shell（subprocess 行被禁用）
    assert "'shell'" not in out.split("工具：")[1].split("\n")[0]
