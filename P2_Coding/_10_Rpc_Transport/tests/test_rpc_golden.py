"""_10_Rpc_Transport 的 golden 测试：协议报文的**稳定快照**（本阶段验收项）。

golden 测试的纪律（与快照测试的通病相对）：

- 快照**写死在文件里**（下面 GOLDEN 常量），不用"首次运行自动生成"的框架——
  人为审核过的形状才是契约；改坏了必须是测试变红，而不是快照被静默更新。
- 快照覆盖两类报文：**帧构造**（protocol 层）与**端到端响应**（服务层，
  跑在 FakeLLM/MemoryFS 上——结果完全确定，包括 final_text）。
- 时间/随机数一概不入帧（服务响应里本来就没有）；包内字段顺序由 json.dumps
  的插入序决定——这正是"形状稳定"的一部分。

一份 golden 报文三处用：客户端实现对接、协议破坏性变更的哨兵、README 示例。
"""

from __future__ import annotations

import io
from pathlib import Path

from config import load_profile
from providers import boot_tree
from server import (
    HarnessService,
    LineTransport,
    error_frame,
    notification_frame,
    result_frame,
)

STAGE_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 帧构造 golden（protocol 层）
# ---------------------------------------------------------------------------

GOLDEN_FRAMES = {
    "result": '{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}',
    "error": '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "参数不对"}}',
    "error_with_data": (
        '{"jsonrpc": "2.0", "id": null, "error": '
        '{"code": -32700, "message": "坏", "data": {"line": 3}}}'
    ),
    "notification": '{"jsonrpc": "2.0", "method": "session.event", "params": {"seq": 5}}',
    "notification_bare": '{"jsonrpc": "2.0", "method": "ping"}',
}


def test_golden_frame_constructors():
    assert result_frame(1, {"ok": True}) == GOLDEN_FRAMES["result"]
    assert error_frame(1, -32602, "参数不对") == GOLDEN_FRAMES["error"]
    assert error_frame(None, -32700, "坏", data={"line": 3}) == GOLDEN_FRAMES["error_with_data"]
    assert (
        notification_frame("session.event", {"seq": 5}) == GOLDEN_FRAMES["notification"]
    )
    assert notification_frame("ping") == GOLDEN_FRAMES["notification_bare"]


# ---------------------------------------------------------------------------
# 服务响应 golden（连续两段：握手与一次 prompt；dev profile 全确定）
# ---------------------------------------------------------------------------


def _serve(lines: list[str], tmp_path: Path) -> list[str]:
    plug_ctx = boot_tree(load_profile("dev", profiles_dir=STAGE_ROOT / "profiles"), stage_root=STAGE_ROOT)
    service = HarnessService(plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    writer = io.StringIO()
    reader = io.StringIO("\n".join(lines) + "\n")
    LineTransport(reader, writer, service).serve_forever()
    return writer.getvalue().splitlines()


def test_golden_initialize_response(tmp_path: Path):
    [line] = _serve(
        ['{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'], tmp_path
    )
    assert line == (
        '{"jsonrpc": "2.0", "id": 1, "result": {"protocol": "harness-jsonrpc/1", '
        '"server": {"name": "mini-harness", "version": "0.2.0"}, '
        '"capabilities": {"methods": ["initialize", "session.prompt"], '
        '"tools": ["calculate", "list_files", "read_file", "shell", "write_file"]}}}'
    )


def test_golden_prompt_response(tmp_path: Path):
    lines = _serve(
        [
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"session.prompt",'
            '"params":{"session":"golden","text":"帮我算 1234*56.78"}}',
        ],
        tmp_path,
    )
    assert lines[1] == (
        '{"jsonrpc": "2.0", "id": 2, "result": {"session": "golden", "turn": 1, '
        '"status": "done", "steps": 2, '
        '"final_text": "1234 × 56.78 = 70066.52。（来自 dev profile 的 FakeLLM）"}}'
    )


def test_golden_error_frames_end_to_end(tmp_path: Path):
    """错误响应也是契约：三类错误（门禁/参数/坏帧）的完整报文形状。"""
    lines = _serve(
        [
            '{"jsonrpc":"2.0","id":1,"method":"session.prompt","params":{"text":"x"}}',
            '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":3,"method":"session.prompt","params":{}}',
            "}}}not-json",
        ],
        tmp_path,
    )
    assert lines[0] == (
        '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, '
        '"message": "服务尚未初始化：请先调用 initialize 完成握手"}}'
    )
    assert lines[2] == (
        '{"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, '
        '"message": "session.prompt 需要非空的 text 参数"}}'
    )
    assert lines[3].startswith('{"jsonrpc": "2.0", "id": null, "error": {"code": -32700')
