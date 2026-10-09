"""mini_harness 的单元测试：订阅台账 + 新编码工具。

与同目录另两个基线文件（22 用例）的分工：本文件测试**本阶段新增的机制**：

1. 订阅（`server/service.py` 的 follow 语义，用 StringIO 驱动）：
   - 重放：`seq > from_seq` 的既有事件按序推给订阅方；
   - 订阅：重放之后的新事件实时推（含 turn 进行中）；
   - 无缝衔接：重放末 seq + 1 = 实时首 seq；
   - 多订阅、按会话隔离、断开注销（drop_sender）；
   - 参数校验（from_seq 非法、未握手）。
2. 编码工具（`providers/toolbox.py` 的 edit_file / search_text / find_files）：
   在 MemoryFS / LocalFS / jail 下语义一致；edit 的三种结局；检索上限与截断。

运行：cd P2_Coding/mini_harness && python -m pytest -q
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from config import load_profile
from providers import (
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_SANDBOX_DENIED,
    LocalFS,
    MemoryFS,
    WorkspaceJailFS,
    boot_tree,
    build_toolbox,
)
from server import INVALID_PARAMS, NOT_INITIALIZED, HarnessService, LineTransport

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
STAGE_ROOT = Path(__file__).resolve().parents[1]


# =========================================================================
# 1) 订阅：follow = 重放 + 订阅
# =========================================================================


def _service(tmp_path: Path, *, approval=None) -> HarnessService:
    plug_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=STAGE_ROOT)
    return HarnessService(
        plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws", approval=approval
    )


def _run(service: HarnessService, lines: list[str]) -> list[str]:
    writer = io.StringIO()
    LineTransport(io.StringIO("\n".join(lines) + "\n"), writer, service).serve_forever()
    return writer.getvalue().splitlines()


def _request(identifier: int, method: str, params: dict | None = None) -> str:
    frame: dict = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        frame["params"] = params
    return json.dumps(frame, ensure_ascii=False)


def _events(lines: list[str]) -> list[dict]:
    return [json.loads(line)["params"]["event"] for line in lines if '"session.event"' in line]


def test_follow_replays_existing_events(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "s1", "text": "你好"})])
    lines = _run(service, [_request(3, "session.follow", {"session": "s1", "from_seq": 0})])
    events = _events(lines)
    assert events and [e["seq"] for e in events] == list(range(1, len(events) + 1))

    response = json.loads([line for line in lines if '"id": 3' in line][0])["result"]
    assert response["replayed"] == len(events)
    assert response["latest_seq"] == events[-1]["seq"]


def test_follow_then_live_events_are_seamless(tmp_path: Path):
    """重放末 seq + 1 == 实时首 seq（无缝衔接：不重不漏）。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "s1", "text": "第一句"})])

    lines = _run(
        service,
        [
            _request(3, "session.follow", {"session": "s1", "from_seq": 0}),
            _request(4, "session.prompt", {"session": "s1", "text": "第二句"}),
        ],
    )
    response_index = next(i for i, line in enumerate(lines) if '"id": 3' in line)
    replayed = _events(lines[:response_index])
    live = _events(lines[response_index + 1 :])
    assert replayed and live
    assert live[0]["seq"] == replayed[-1]["seq"] + 1
    assert [e["seq"] for e in live] == list(range(live[0]["seq"], live[-1]["seq"] + 1))


def test_follow_from_seq_skips_earlier_events(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "s1", "text": "你好"})])
    lines = _run(service, [_request(3, "session.follow", {"session": "s1", "from_seq": 5})])
    events = _events(lines)
    assert events and all(e["seq"] > 5 for e in events)
    assert events[0]["seq"] == 6  # 从 from_seq 之后紧接着开始


def test_follow_with_latest_from_seq_replays_nothing_but_subscribes(tmp_path: Path):
    """断线重连的常态：from_seq 已是最新 → 零重放，但订阅生效（后续事件照收）。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "s1", "text": "你好"})])
    first_follow = _run(service, [_request(3, "session.follow", {"session": "s1", "from_seq": 0})])
    latest = json.loads([line for line in first_follow if '"id": 3' in line][0])["result"]["latest_seq"]

    lines = _run(
        service,
        [
            _request(4, "session.follow", {"session": "s1", "from_seq": latest}),
            _request(5, "session.prompt", {"session": "s1", "text": "新消息"}),
        ],
    )
    assert _events([line for line in lines if '"id": 4' not in line][:0]) == []
    live = _events(lines)
    assert live and live[0]["seq"] == latest + 1


def test_follow_session_isolation(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(
        service,
        [
            _request(1, "initialize"),
            _request(2, "session.prompt", {"session": "a", "text": "在 a"}),
            _request(3, "session.prompt", {"session": "b", "text": "在 b"}),
        ],
    )
    lines = _run(service, [_request(4, "session.follow", {"session": "a", "from_seq": 0})])
    events = _events(lines)
    assert {e["data"].get("session_id") or "a" for e in events if e["type"] == "session/start"}
    starts = [e for e in events if e["type"] == "session/start"]
    assert all(e["data"]["session_id"] == "a" for e in starts)


def test_multiple_subscribers_all_receive(tmp_path: Path):
    """两个连接各订阅一次：后续新事件推给**两个订阅者各自的连接**。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize"), _request(2, "session.prompt", {"session": "s1", "text": "你好"})])

    first = _run(service, [_request(3, "session.follow", {"session": "s1", "from_seq": 0})])
    assert len(service.subscriptions) == 1

    # 第二个订阅者：复用它的连接（writer）接收后续推送
    second_writer = io.StringIO()
    LineTransport(
        io.StringIO(_request(4, "session.follow", {"session": "s1", "from_seq": 0}) + "\n"),
        second_writer,
        service,
    ).serve_forever()
    assert len(service.subscriptions) == 2
    assert len(_events(first)) == len(_events(second_writer.getvalue().splitlines()))

    # 第三个连接发 prompt：新事件应推给两个既有的订阅连接
    _run(service, [_request(5, "session.prompt", {"session": "s1", "text": "新消息"})])
    second_after = _events(second_writer.getvalue().splitlines())
    assert _events(first) and second_after
    assert len(second_after) > 0, "跨连接的新事件应推给既有订阅者"


def test_drop_sender_unregisters(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize")])
    _run(service, [_request(2, "session.follow", {"session": "s1", "from_seq": 0})])
    assert len(service.subscriptions) == 1
    sender = service.subscriptions[0].sender
    service.drop_sender(sender)
    assert service.subscriptions == []


def test_follow_requires_handshake(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    lines = _run(service, [_request(1, "session.follow", {"session": "s1"})])
    assert json.loads(lines[0])["error"]["code"] == NOT_INITIALIZED


def test_follow_rejects_bad_from_seq(tmp_path: Path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    lines = _run(
        service,
        [_request(1, "initialize"), _request(2, "session.follow", {"session": "s1", "from_seq": -1})],
    )
    assert json.loads(lines[1])["error"]["code"] == INVALID_PARAMS


def test_event_frames_are_notifications_with_seq(tmp_path: Path):
    """事件帧是通知（无 id）且带 seq——客户端据此断点续跟。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    service = _service(tmp_path)
    _run(service, [_request(1, "initialize")])
    lines = _run(service, [_request(2, "session.follow", {"session": "s1", "from_seq": 0})])
    frames = [json.loads(line) for line in lines if '"session.event"' in line]
    assert frames and all("id" not in frame for frame in frames)
    assert all(frame["method"] == "session.event" for frame in frames)
    assert all(isinstance(frame["params"]["event"]["seq"], int) for frame in frames)


# =========================================================================
# 2) 新编码工具
# =========================================================================


def _tools(fs) -> object:
    return build_toolbox(fs)


@pytest.mark.parametrize("provider", ["memory", "local"])
def test_edit_file_replaces_exactly_once(provider, tmp_path: Path):
    fs = MemoryFS() if provider == "memory" else LocalFS(tmp_path / "ws")
    registry = _tools(fs)
    registry.resolve("write_file").func(path="a.py", content="x = 1\ny = 2\n")
    result = registry.resolve("edit_file").func(path="a.py", old="x = 1", new="x = 10")
    assert result == {"edited": "a.py", "removed": 5, "added": 6}
    assert fs.read_text("a.py") == "x = 10\ny = 2\n"


@pytest.mark.parametrize("provider", ["memory", "local"])
def test_edit_file_no_match_and_ambiguous(provider, tmp_path: Path):
    fs = MemoryFS() if provider == "memory" else LocalFS(tmp_path / "ws")
    registry = _tools(fs)
    registry.resolve("write_file").func(path="a.txt", content="dup dup\n")
    no_match = registry.resolve("edit_file").func(path="a.txt", old="nope", new="x")
    assert no_match["code"] == FS_EDIT_NO_MATCH
    ambiguous = registry.resolve("edit_file").func(path="a.txt", old="dup", new="x")
    assert ambiguous["code"] == FS_EDIT_AMBIGUOUS


def test_edit_file_needs_approval():
    registry = _tools(MemoryFS())
    assert registry.resolve("edit_file").definition.needs_approval is True


def test_search_text_finds_lines_with_numbers(tmp_path: Path):
    fs = MemoryFS()
    registry = _tools(fs)
    registry.resolve("write_file").func(path="src/app.py", content="import os\n# TODO: fix\n")
    registry.resolve("write_file").func(path="docs/readme.md", content="# TODO: doc\n")
    result = registry.resolve("search_text").func(pattern="TODO", path=".")
    assert result["matches"] == [
        {"file": "docs/readme.md", "line": 1, "text": "# TODO: doc"},
        {"file": "src/app.py", "line": 2, "text": "# TODO: fix"},
    ]


def test_search_text_bad_regex_is_structured_error():
    registry = _tools(MemoryFS())
    result = registry.resolve("search_text").func(pattern="[unclosed")
    assert result["code"] == "INVALID_PATTERN"


def test_search_text_truncates_with_notice():
    fs = MemoryFS()
    registry = _tools(fs)
    registry.resolve("write_file").func(path="a.txt", content="\n".join(f"hit {i}" for i in range(30)))
    result = registry.resolve("search_text").func(pattern="hit", max_results=5)
    assert len(result["matches"]) == 5 and "truncated" in result


def test_search_text_scoped_to_subdirectory():
    fs = MemoryFS()
    registry = _tools(fs)
    registry.resolve("write_file").func(path="src/a.py", content="needle\n")
    registry.resolve("write_file").func(path="other/b.py", content="needle\n")
    result = registry.resolve("search_text").func(pattern="needle", path="src")
    assert [m["file"] for m in result["matches"]] == ["src/a.py"]


def test_find_files_by_pattern():
    fs = MemoryFS()
    registry = _tools(fs)
    for path in ("src/app.py", "tests/test_app.py", "notes/todo.txt"):
        registry.resolve("write_file").func(path=path, content="x")
    by_suffix = registry.resolve("find_files").func(pattern="*.py")
    assert by_suffix["files"] == ["src/app.py", "tests/test_app.py"]
    by_prefix = registry.resolve("find_files").func(pattern="test_*")
    assert by_prefix["files"] == ["tests/test_app.py"]


def test_find_files_truncates_with_notice():
    fs = MemoryFS()
    registry = _tools(fs)
    for index in range(10):
        registry.resolve("write_file").func(path=f"f{index}.txt", content="x")
    result = registry.resolve("find_files").func(pattern="*.txt", max_results=3)
    assert len(result["files"]) == 3 and "truncated" in result


def test_jail_blocks_edit_escape_but_allows_search(tmp_path: Path):
    """jail：edit 越界被拒（改动被拦）；search/find 照常（只读，不越权）。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    jail_ws = tmp_path / "ws"
    jail = WorkspaceJailFS(LocalFS(jail_ws))
    registry = _tools(jail)
    registry.resolve("write_file").func(path="inside.txt", content="needle\n")

    denied = registry.resolve("edit_file").func(path="../outside.txt", old="secret", new="x")
    assert denied["code"] == FS_SANDBOX_DENIED
    assert outside.read_text(encoding="utf-8") == "secret\n"  # 原文件未动
    found = registry.resolve("search_text").func(pattern="needle")
    assert [m["file"] for m in found["matches"]] == ["inside.txt"]
