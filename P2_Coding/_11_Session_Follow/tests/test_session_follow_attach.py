"""_11_Session_Follow 的入口测试：attach.py 客户端（含 stdio 实跑与渲染）。

`attach.py` 是本阶段的客户端入口。本文件测试：

1. `render_event`：事件 → 一行人话的映射（含工具结果截断、turn 边界）；
2. **stdio 实跑**：`AttachClient` 拉起 serve.py 子进程，`--ask` 一条消息拿到回答，
   同时收到事件流（重放 + 实时）；
3. **TCP 实跑**：连一个真的 TcpServer，`AttachClient.ask` 走网络；
4. 断线重连状态机：`last_seq` 随事件推进、重连后 from_seq 用它（wiring 里已有
   TCP 全链验证，这里测客户端把 last_seq 管对）。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from attach import AttachClient, render_event

from config import load_profile
from providers import boot_tree
from server import HarnessService, make_server

STAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILES = STAGE_ROOT / "profiles"


# =========================================================================
# 1) 事件渲染
# =========================================================================


def test_render_event_user_and_assistant():
    assert "你：你好" in render_event({"seq": 3, "type": "user/message", "data": {"content": "你好"}})
    line = render_event(
        {"seq": 5, "type": "assistant/message", "data": {"content": "好的", "tool_calls": []}}
    )
    assert "助手：好的" in line


def test_render_event_tool_calls_and_results():
    line = render_event(
        {
            "seq": 5,
            "type": "assistant/message",
            "data": {
                "content": "",
                "tool_calls": [{"id": "c1", "name": "calculate", "arguments": {}}],
            },
        }
    )
    assert "申请调用工具：calculate" in line
    result_line = render_event(
        {
            "seq": 6,
            "type": "tool/result",
            "data": {"name": "calculate", "result": {"result": 42}, "is_error": False},
        }
    )
    assert "✓ calculate" in result_line and "42" in result_line


def test_render_event_truncates_long_tool_result():
    line = render_event(
        {
            "seq": 6,
            "type": "tool/result",
            "data": {"name": "read_file", "result": {"content": "x" * 1000}, "is_error": False},
        }
    )
    assert line is not None and len(line) < 260 and "…" in line


def test_render_event_turn_boundaries_and_quiet_events():
    assert "turn 1" in render_event({"seq": 2, "type": "turn/start", "data": {"turn": 1}})
    assert "结束：done" in render_event(
        {"seq": 9, "type": "turn/end", "data": {"turn": 1, "status": "done"}}
    )
    # 默认安静的事件（verbose 才显示）
    assert render_event({"seq": 1, "type": "session/start", "data": {}}) is None
    assert render_event({"seq": 1, "type": "session/start", "data": {}}, verbose=True) is not None


# =========================================================================
# 2) stdio 实跑：AttachClient 拉起 serve.py 子进程
# =========================================================================


def test_attach_stdio_ask_end_to_end(tmp_path: Path):
    client = AttachClient(
        session="attach-stdio",
        serve_args=[
            "--profile", "dev",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ],
    )
    handshake = client.start()
    assert handshake["protocol"] == "harness-jsonrpc/1"
    assert "edit_file" in handshake["capabilities"]["tools"]  # 新工具在能力清单里

    response = client.ask("你好", timeout=60)
    result = response["result"]
    assert result["status"] == "done" and result["turn"] == 1
    # 跟随：last_seq 已被事件推进（说明事件真的推来了）
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and client.last_seq == 0:
        time.sleep(0.05)
    assert client.last_seq > 0
    client.stop()


def test_attach_cli_ask_prints_reply(tmp_path: Path):
    """命令行 --ask：子进程实跑 attach.py，输出里含助手回复与 [attach] 提示。"""
    completed = subprocess.run(
        [
            sys.executable,
            str(STAGE_ROOT / "attach.py"),
            "--profile", "dev",
            "--session", "cli-attach",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
            "--ask", "你好",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert completed.returncode == 0
    assert "助手>" in completed.stdout
    assert "[attach] 已连接" in completed.stderr


# =========================================================================
# 3) TCP 实跑：连常驻服务
# =========================================================================


def test_attach_over_tcp(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    plug_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=STAGE_ROOT)
    service = HarnessService(plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws")
    server = make_server("127.0.0.1", 0, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    client = AttachClient(session="via-tcp", connect=f"{host}:{port}")
    try:
        handshake = client.start()
        assert handshake["protocol"] == "harness-jsonrpc/1"
        response = client.ask("你好", timeout=60)
        assert response["result"]["turn"] == 1
        assert client.last_seq > 0
    finally:
        client.stop()
        server.shutdown()
        server.server_close()


def test_attach_over_tcp_resumes_with_last_seq(tmp_path: Path):
    """TCP 下断线重连：新客户端用旧 last_seq 跟随，只补缺口（见 wiring 的同款断言）。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    plug_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=STAGE_ROOT)
    service = HarnessService(plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws")
    server = make_server("127.0.0.1", 0, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]

    first = AttachClient(session="resume-tcp", connect=f"{host}:{port}", auto_reconnect=False)
    try:
        first.start()
        first.ask("第一句", timeout=60)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and first.last_seq == 0:
            time.sleep(0.05)
        seen = first.last_seq
        assert seen > 0
    finally:
        first.stop()

    # 第二个客户端从 seen 继续：重放的事件 seq 全部 > seen
    second = AttachClient(
        session="resume-tcp", from_seq=seen, connect=f"{host}:{port}", auto_reconnect=False
    )
    try:
        second.start()
        second.ask("第二句", timeout=60)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and second.last_seq <= seen:
            time.sleep(0.05)
        assert second.last_seq > seen  # 推进过了断点
    finally:
        second.stop()
        server.shutdown()
        server.server_close()
