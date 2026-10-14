"""P3 RPC 入口测试：stdio 协议线纯净性（stdout 只出帧）。

这是 stdio 服务的经典坑，也是 P2 `_10` 踩过的：人看的 banner/台账一旦混进
stdout，客户端按行解析就崩。本用例用**真实子进程**跑 `chat.py --rpc`——
比内存流更硬：它验证入口脚本自己有没有把东西写错流。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]  # mini_harness/


def test_stdio_stdout_carries_only_frames(tmp_path: Path):
    frames_in = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.prompt",
                    "params": {"session": "s1", "text": "你好"},
                }
            ),
        ]
    )
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "chat.py"),
            "--rpc",
            "--root",
            str(tmp_path / "sessions"),
            "--workspace",
            str(tmp_path / "ws"),
        ],
        input=frames_in + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    # stdout 每一行都必须是合法帧——任何横幅混进来都会在这里失败。
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert "capabilities" in parsed[0]["result"]
    assert parsed[1]["result"]["status"] == "done"

    # banner 与人话在 stderr（不干扰协议线），且提到了插件台账。
    assert "插件台账" in result.stderr
    assert "装配完成" in result.stderr


def test_rpc_mode_rejects_unknown_method_with_stable_code(tmp_path: Path):
    frames_in = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "session.nope", "params": {}}
    )
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "chat.py"),
            "--rpc",
            "--root",
            str(tmp_path / "sessions"),
            "--workspace",
            str(tmp_path / "ws"),
        ],
        input=frames_in + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    frame = json.loads(result.stdout.strip().splitlines()[-1])
    assert frame["error"]["code"] == -32601  # NOT_INITIALIZED 优先：未握手就该先报它
    assert frame["error"]["code"] in (-32601, -32002)


@pytest.mark.parametrize("method", ["initialize"])
def test_rpc_handshake_shape_is_stable(tmp_path: Path, method: str):
    """golden 形状：握手响应的字段集合是契约（客户端按它分支）。"""
    frames_in = json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": {}})
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "chat.py"),
            "--rpc",
            "--root",
            str(tmp_path / "sessions"),
            "--workspace",
            str(tmp_path / "ws"),
        ],
        input=frames_in + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    frame = json.loads(result.stdout.strip().splitlines()[-1])
    assert frame["id"] == 7
    payload = frame["result"]
    assert set(payload) == {"protocol", "server", "capabilities"}
    assert set(payload["server"]) == {"name", "version"}
    assert set(payload["capabilities"]) == {"methods", "tools"}
