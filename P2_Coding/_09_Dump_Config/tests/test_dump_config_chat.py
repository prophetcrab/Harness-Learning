"""_09_Dump_Config 的入口测试：`chat.py`（离线，非交互）。

`chat.py` 的 `--dump-config` 现在输出**带来源**的配置树；配置错误带定位并
以退出码 2 报错。本文件测试：

1. `--dump-config`：含来源箭头与层名（base.yaml / dev.yaml / CLI#N …）；
2. 暴露给用户的错误文案：带层名/行 id/拼写建议，且不抛栈；
3. `--ask` 端到端（dev profile）仍照常工作（配置层改动没破坏会话路径）。

运行：cd P2_Coding/_09_Dump_Config && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

from chat import main


def test_dump_config_shows_sources(capsys):
    rc = main(["--profile", "dev", "--dump-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "← dev.yaml" in out          # dev 动过的三行
    assert "← base.yaml" in out         # 没动过的 toolbox 行
    assert "config.shell_timeout" in out
    assert "激活 4 行" in out


def test_dump_config_with_cli_patch_shows_numbered_layer(tmp_path: Path, capsys):
    cli = tmp_path / "my.patch.yaml"
    cli.write_text("patch:\n  - id: llm\n    name: llm:deepseek\n", encoding="utf-8")
    rc = main(["--profile", "dev", "--patch", str(cli), "--dump-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "← CLI#1 my.patch.yaml" in out


def test_unknown_row_id_reports_layer_position_and_hint(tmp_path: Path, capsys):
    cli = tmp_path / "bad.yaml"
    cli.write_text("patch:\n  - id: toolbx\n    disabled: true\n", encoding="utf-8")
    rc = main(["--profile", "dev", "--patch", str(cli), "--dump-config"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "[配置错误]" in out
    assert "bad.yaml 第 1 条 patch" in out
    assert "你是不是想改 'toolbox'" in out


def test_unknown_plugin_name_reports_row_and_hint(tmp_path: Path, capsys):
    cli = tmp_path / "bad.yaml"
    cli.write_text("patch:\n  - id: llm\n    name: llm:fake2\n", encoding="utf-8")
    rc = main(["--profile", "dev", "--patch", str(cli)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "[激活失败]" in out
    assert "行 'llm'" in out and "llm:fake2" in out
    assert "你是不是想写 'llm:fake'" in out


def test_unknown_config_key_reports_row_and_hint(tmp_path: Path, capsys):
    cli = tmp_path / "bad.yaml"
    cli.write_text(
        "patch:\n  - id: toolbox\n    config:\n      shell_timeou: 3\n", encoding="utf-8"
    )
    rc = main(["--profile", "dev", "--patch", str(cli)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "shell_timeou" in out
    assert "你是不是想写 'shell_timeout'" in out


def test_bad_yaml_reports_file(capsys):
    """坏 YAML：错误信息带文件路径（定位到"哪个文件哪一行"）。"""
    rc = main(["--profile", "dev", "--dump-config"])
    assert rc == 0  # 正常路径先确认没坏

    import pytest
    from chat import load_and_boot

    from config import ConfigError

    bad_dir = Path(__file__).resolve().parents[1] / "demo_run"
    bad_dir.mkdir(parents=True, exist_ok=True)
    bad = bad_dir / "broken.yaml"
    bad.write_text("patch: [oops\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_and_boot("dev", [str(bad)])
    assert "broken.yaml" in str(info.value) and "不是合法 YAML" in str(info.value)


def test_ask_still_works_after_config_changes(tmp_path: Path, capsys):
    """配置层改动（来源追踪）没有破坏会话路径：dev profile 照常跑一个 turn。"""
    rc = main(
        [
            "--ask", "帮我算 1+1",
            "--profile", "dev",
            "--no-approve",
            "--session", "cfg-still-ok",
            "--root", str(tmp_path / "sessions"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "profile：dev" in out
    assert "整体卸载后 ctx 槽位：[]" in out
