"""mini_harness 的 golden 测试：follow 的报文形状与顺序（本阶段验收项）。

golden 纪律与前两阶段一致：快照**写死**在测试里，不用自动生成框架——
"follow = 重放 + 订阅"的顺序（重放先于响应、实时事件 seq 接在重放之后）
是协议契约，改坏了必须测试变红。

两段 golden：

1. `session.event` 通知的**帧形状**（方法名 / params 结构 / 事件字段）；
2. `session.follow` 的**响应形状**与**帧序**（重放 → 响应 → 实时），
   以及 `from_seq` 的过滤语义（`seq > from_seq`）。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from config import load_profile
from providers import boot_tree
from server import HarnessService, LineTransport

STAGE_ROOT = Path(__file__).resolve().parents[1]


def _service(tmp_path: Path) -> HarnessService:
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    plug_ctx = boot_tree(
        load_profile("dev", profiles_dir=STAGE_ROOT / "profiles"), stage_root=STAGE_ROOT
    )
    return HarnessService(plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws")


def _run(service: HarnessService, lines: list[str]) -> list[str]:
    writer = io.StringIO()
    LineTransport(io.StringIO("\n".join(lines) + "\n"), writer, service).serve_forever()
    return writer.getvalue().splitlines()


def _request(identifier: int, method: str, params: dict | None = None) -> str:
    frame: dict = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        frame["params"] = params
    return json.dumps(frame, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1) 事件通知的帧形状
# ---------------------------------------------------------------------------

GOLDEN_EVENT_FRAME_KEYS = {"jsonrpc", "method", "params"}


def test_golden_session_event_frame_shape(tmp_path: Path):
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "g", "text": "你好"})])
    lines = _run(service, [_request(3, "session.follow", {"session": "g", "from_seq": 0})])
    frames = [json.loads(line) for line in lines if '"session.event"' in line]

    first = frames[0]
    assert set(first) == GOLDEN_EVENT_FRAME_KEYS                    # 通知帧：无 id
    assert first["jsonrpc"] == "2.0"
    assert first["method"] == "session.event"
    params = first["params"]
    assert set(params) == {"session", "event"}
    assert params["session"] == "g"
    event = params["event"]
    assert set(event) == {"seq", "type", "data", "ts"}              # Event.to_dict 的形状
    assert event["seq"] == 1 and event["type"] == "session/start"
    assert isinstance(event["data"], dict) and isinstance(event["ts"], str)


# ---------------------------------------------------------------------------
# 2) follow：响应形状与"重放 → 响应 → 实时"的帧序
# ---------------------------------------------------------------------------


def test_golden_follow_response_shape(tmp_path: Path):
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "g", "text": "你好"})])
    lines = _run(service, [_request(3, "session.follow", {"session": "g", "from_seq": 0})])
    response = json.loads([line for line in lines if json.loads(line).get("id") == 3][0])
    assert set(response["result"]) == {"session", "from_seq", "replayed", "latest_seq", "subscription"}
    assert response["result"]["session"] == "g"
    assert response["result"]["from_seq"] == 0
    assert response["result"]["latest_seq"] == response["result"]["replayed"]  # 从头重放时两者一致


def test_golden_frame_order_replay_then_response_then_live(tmp_path: Path):
    """帧序契约：重放事件在前 → follow 响应 → 实时事件在后（seq 严格递增）。"""
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "g", "text": "第一句"})])
    lines = _run(
        service,
        [
            _request(3, "session.follow", {"session": "g", "from_seq": 0}),
            _request(4, "session.prompt", {"session": "g", "text": "第二句"}),
        ],
    )
    kinds = [
        ("response" if json.loads(line).get("id") == 3 else
         "event" if '"session.event"' in line else "response")
        for line in lines
    ]
    # 响应恰好出现一次；它之前全是事件（重放），之后的事件是实时推送
    assert kinds.count("response") == 2  # follow 响应 + prompt 响应
    follow_index = next(i for i, line in enumerate(lines) if json.loads(line).get("id") == 3)
    assert all(kind == "event" for kind in kinds[:follow_index])
    assert kinds[follow_index] == "response"
    assert any(kind == "event" for kind in kinds[follow_index + 1 :])
    seqs = [json.loads(line)["params"]["event"]["seq"] for line in lines if '"session.event"' in line]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_golden_from_seq_filter(tmp_path: Path):
    """from_seq 的过滤语义：只重放 seq > from_seq。"""
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "g", "text": "你好"})])
    all_events = _run(service, [_request(3, "session.follow", {"session": "g", "from_seq": 0})])
    total = len([line for line in all_events if '"session.event"' in line])
    assert total > 2

    lines = _run(service, [_request(4, "session.follow", {"session": "g", "from_seq": 2})])
    replayed = [
        json.loads(line)["params"]["event"]["seq"]
        for line in lines
        if '"session.event"' in line
    ]
    assert replayed == list(range(3, total + 1))  # 严格 > 2，直到最新
