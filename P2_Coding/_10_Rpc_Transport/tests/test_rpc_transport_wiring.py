"""_10_Rpc_Transport 的接线测试：服务端到端 + serve.py 子进程。

验收映射（本阶段 README）：
- 「换行 JSON-RPC 收发稳定」→ 批量子进程往返（真实 stdio，见下）+ 混合帧序列；
- 「initialize 握手」→ 握手前门禁、握手后可用、重复握手幂等；
- 「session.prompt 落盘」→ 服务响应 与 JSONL 日志双向验证（turn 接续）；
- 「审批 fail-closed」→ 服务端无应答方时需审批工具被拒（AutoDeny 默认）。

运行：cd P2_Coding/_10_Rpc_Transport && python -m pytest -q
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from config import load_profile
from harness.session import JsonlStore
from harness.tools import AutoApprove, AutoDeny
from providers import boot_tree
from server import HarnessService, LineTransport

STAGE_ROOT = Path(__file__).resolve().parents[1]


def _service(tmp_path: Path, *, approval=None) -> HarnessService:
    plug_ctx = boot_tree(
        load_profile("dev", profiles_dir=STAGE_ROOT / "profiles"), stage_root=STAGE_ROOT
    )
    return HarnessService(
        plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws", approval=approval
    )


def _serve(service: HarnessService, lines: list[str]) -> list[str]:
    writer = io.StringIO()
    reader = io.StringIO("\n".join(lines) + "\n")
    LineTransport(reader, writer, service).serve_forever()
    return writer.getvalue().splitlines()


def _request(identifier: int, method: str, params: dict | None = None) -> str:
    frame: dict = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        frame["params"] = params
    return json.dumps(frame, ensure_ascii=False)


def _parse(line: str) -> dict:
    return json.loads(line)


# =========================================================================
# 1) 握手
# =========================================================================


def test_initialize_handshake(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    [line] = _serve(service, [_request(1, "initialize")])
    result = _parse(line)["result"]
    assert result["protocol"] == "harness-jsonrpc/1"
    assert result["server"]["name"] == "mini-harness"
    assert "session.prompt" in result["capabilities"]["methods"]
    assert "shell" in result["capabilities"]["tools"]  # 能力清单 = 真实工具面


def test_prompt_before_initialize_is_rejected(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    [line] = _serve(service, [_request(1, "session.prompt", {"text": "hi"})])
    assert _parse(line)["error"]["code"] == -32002


def test_initialize_is_idempotent(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    lines = _serve(service, [_request(1, "initialize"), _request(2, "initialize")])
    assert _parse(lines[0])["result"] == _parse(lines[1])["result"]


# =========================================================================
# 2) session.prompt：结果、落盘、会话接续
# =========================================================================


def test_prompt_runs_turn_and_persists(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path, approval=AutoApprove())
    lines = _serve(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "s1", "text": "帮我算 1234*56.78"}),
        ],
    )
    result = _parse(lines[1])["result"]
    assert result["turn"] == 1 and result["status"] == "done"
    assert "70066.52" in result["final_text"]

    events, _ = JsonlStore(tmp_path / "sessions").load("s1")
    types = [e.type for e in events]
    assert "session/start" in types and "tool/result" in types  # 落盘且含工具往返
    assert any(e.type == "turn/end" for e in events)


def test_same_session_continues_turn_number(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path, approval=AutoApprove())
    lines = _serve(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "s1", "text": "第一句"}),
            _request(3, "session.prompt", {"session": "s1", "text": "第二句"}),
        ],
    )
    assert _parse(lines[1])["result"]["turn"] == 1
    assert _parse(lines[2])["result"]["turn"] == 2  # 同名会话接续
    # 历史在日志里是连续的（两次 turn）
    events, _ = JsonlStore(tmp_path / "sessions").load("s1")
    assert sum(1 for e in events if e.type == "turn/start") == 2


def test_new_session_is_independent(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path, approval=AutoApprove())
    lines = _serve(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "a", "text": "x"}),
            _request(3, "session.prompt", {"session": "b", "text": "y"}),
        ],
    )
    assert _parse(lines[1])["result"]["session"] == "a"
    assert _parse(lines[2])["result"]["turn"] == 1  # b 是新会话
    assert set(service.open_sessions) == {"a", "b"}


# =========================================================================
# 3) 审批 fail-closed（服务端无人工应答方）
# =========================================================================


def test_approval_is_fail_closed_by_default(tmp_path: Path):
    """dev 的 FakeLLM 剧本第一幕是 calculate（无需审批）；第二幕是 shell（需审批）。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)  # 不传 approval → 默认 AutoDeny
    lines = _serve(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "fc", "text": "第一句"}),
            _request(3, "session.prompt", {"session": "fc", "text": "用 shell 跑一下"}),
        ],
    )
    assert _parse(lines[2])["result"]["status"] == "done"  # 服务不炸
    events, _ = JsonlStore(tmp_path / "sessions").load("fc")
    denied = [e for e in events if e.type == "tool/result" and e.data["is_error"]]
    assert denied, "默认应拒绝需审批操作"
    assert "fail-closed" in denied[0].data["result"]["error"]


def test_explicit_approve_allows_tool(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path, approval=AutoApprove())
    _serve(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "ok", "text": "第一句"}),
            _request(3, "session.prompt", {"session": "ok", "text": "用 shell 跑一下"}),
        ],
    )
    events, _ = JsonlStore(tmp_path / "sessions").load("ok")
    shell_results = [e.data for e in events if e.type == "tool/result" and e.data["name"] == "shell"]
    assert shell_results and shell_results[0]["is_error"] is False


def test_custom_deny_reason_reaches_log(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path, approval=AutoDeny("维护窗口：全部拒绝"))
    _serve(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "d", "text": "第一句"}),
            _request(3, "session.prompt", {"session": "d", "text": "用 shell 跑一下"}),
        ],
    )
    events, _ = JsonlStore(tmp_path / "sessions").load("d")
    denied = [e for e in events if e.type == "tool/result" and e.data["is_error"]]
    assert denied and "维护窗口" in denied[0].data["result"]["error"]


# =========================================================================
# 4) 参数与服务级错误的形状
# =========================================================================


def test_prompt_requires_text(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    lines = _serve(
        service,
        [_request(1, "initialize"), _request(2, "session.prompt", {"session": "x"})],
    )
    assert _parse(lines[1])["error"]["code"] == -32602


def test_unknown_method_lists_available(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    lines = _serve(service, [_request(1, "session.follow", {"from": 0})])
    error = _parse(lines[0])["error"]
    assert error["code"] == -32601
    assert "initialize" in error["message"]  # 列出可用方法（session.follow 是 _11 的）


# =========================================================================
# 5) serve.py 子进程：真实 stdio 往返（验收"收发稳定"）
# =========================================================================

SERVE_SCRIPT = STAGE_ROOT / "serve.py"


def test_serve_subprocess_stdio_round_trip(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "sub", "text": "帮我算 1+1"}),
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SERVE_SCRIPT),
            "--profile", "dev",
            "--root", str(tmp_path / "sessions"),
            "--workspace", str(tmp_path / "ws"),
        ],
        input=payload + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0
    # stdout 只有帧（banner 在 stderr）
    out_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(out_lines) == 2
    assert _parse(out_lines[0])["result"]["protocol"] == "harness-jsonrpc/1"
    assert _parse(out_lines[1])["result"]["turn"] == 1
    assert "[serve]" in completed.stderr
    assert (tmp_path / "sessions" / "sub" / "session.jsonl").is_file()
