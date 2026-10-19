"""会话日志的持久化语义：时间戳 / header / 目录布局 / stat·list / 未知事件 / flush。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import Context
from plugins.base.sessions import (
    KNOWN_EVENT_TYPES,
    SESSION_START,
    SESSIONS_SERVICE,
    TURN_START,
    SessionEvent,
    UnknownEventTypeError,
    sessions_plugin,
)
from plugins.capability.session.store_jsonl import (
    FORMAT_VERSION,
    SESSION_STORE_SERVICE,
    SessionFormatError,
    SessionHeader,
    SessionReader,
    SessionWriter,
    encode_session_id,
    project_dir_name,
    session_path,
    session_store_jsonl_plugin,
    truncate_torn_tail,
)


def booted(root: Path) -> tuple[Context, object, object]:
    ctx = Context()
    ctx.load_plugin(sessions_plugin())
    ctx.load_plugin(session_store_jsonl_plugin(root=root))
    return ctx, ctx.require(SESSIONS_SERVICE), ctx.require(SESSION_STORE_SERVICE)


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------


def test_every_event_gets_a_timestamp():
    ctx, sessions, _store = booted(Path("/tmp/unused"))
    sessions.open("s1")
    event = sessions.append("s1", TURN_START, {"turn": 1, "text": "hi"})
    assert event.at.endswith("+00:00")
    assert "T" in event.at
    # 后一条不早于前一条
    second = sessions.append("s1", TURN_START, {"turn": 2, "text": "again"})
    assert second.at >= event.at


def test_explicit_timestamp_is_kept():
    ctx, sessions, _store = booted(Path("/tmp/unused"))
    sessions.open("s1")
    event = sessions.append("s1", TURN_START, {"turn": 1, "text": "hi"}, at="2020-01-01T00:00:00+00:00")
    assert event.at == "2020-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 目录布局 / header
# ---------------------------------------------------------------------------


def test_project_dir_is_readable_and_stable():
    assert project_dir_name("/d/project/x") == "--d_project_x--"
    # Windows 盘符里的 `:` 被替换成 `_`，所以是 `D_` + `_project` = `D__project`
    assert project_dir_name("D:\\project\\x") == "--D__project_x--"
    assert project_dir_name(None) == "_no-cwd_"
    assert project_dir_name("/") == "--root--"


def test_session_id_is_injectively_encoded():
    assert encode_session_id("a/b") != encode_session_id("a_b")
    assert "/" not in encode_session_id("a/b")
    assert encode_session_id("s1") == "s1"


def test_disk_layout_has_header_and_versioned_file(tmp_path: Path):
    ctx, sessions, store = booted(tmp_path)
    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "hi"})
    store.flush()

    path = store.path_for("s1")
    assert path.name == f"session.v{FORMAT_VERSION}.jsonl"
    assert path.parent.name == "s1"
    assert path.parent.parent.name == "_no-cwd_"

    header = SessionReader(path).header()
    assert header.session_id == "s1"
    assert header.version == FORMAT_VERSION
    assert header.created_at

    # 首行确实是 header
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["format"] == "p4-session"
    assert first["version"] == FORMAT_VERSION


def test_unsupported_format_version_is_rejected(tmp_path: Path):
    path = tmp_path / "session.v1.jsonl"
    path.write_text(
        json.dumps({"format": "p4-session", "version": 99, "session_id": "x"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="不支持的会话格式版本"):
        SessionReader(path).header()


def test_wrong_format_is_rejected(tmp_path: Path):
    path = tmp_path / "session.v1.jsonl"
    path.write_text('{"format": "other"}\n', encoding="utf-8")
    with pytest.raises(SessionFormatError, match="不是本项目的会话日志"):
        SessionReader(path).header()


# ---------------------------------------------------------------------------
# stat / list 只读 header
# ---------------------------------------------------------------------------


def test_stat_does_not_read_event_lines(tmp_path: Path, monkeypatch):
    ctx, sessions, store = booted(tmp_path)
    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "hi"})
    store.flush()

    called = {"events": 0}
    original = SessionReader.events

    def counting(self):
        called["events"] += 1
        return original(self)

    monkeypatch.setattr(SessionReader, "events", counting)
    snapshot = store.stat("s1")
    assert snapshot["session_id"] == "s1"
    assert snapshot["size_bytes"] > 0
    assert called["events"] == 0  # 没有读事件行

    listed = store.list()
    assert [item["session_id"] for item in listed] == ["s1"]
    assert called["events"] == 0


def test_stat_missing_session_returns_none(tmp_path: Path):
    _ctx, _sessions, store = booted(tmp_path)
    assert store.stat("nope") is None


def test_list_includes_cwd_and_project_directory(tmp_path: Path):
    from core import Context as Ctx
    from plugins.base.workspace import workspace_plugin

    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = Ctx()
    ctx.load_plugin(workspace_plugin(root=ws))
    ctx.load_plugin(sessions_plugin())
    ctx.load_plugin(session_store_jsonl_plugin(root=tmp_path / "logs"))
    sessions = ctx.require(SESSIONS_SERVICE)
    store = ctx.require(SESSION_STORE_SERVICE)

    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "hi"})
    store.flush()

    listed = store.list()
    assert listed[0]["session_id"] == "s1"
    assert listed[0]["cwd"] == str(ws.resolve())
    assert listed[0]["project"] == project_dir_name(str(ws.resolve()))
    assert listed[0]["revision"].count(":") == 1


# ---------------------------------------------------------------------------
# 未知事件类型
# ---------------------------------------------------------------------------


def test_unknown_event_without_marker_is_rejected():
    data = {"seq": 1, "kind": "future/thing", "payload": {}, "at": ""}
    with pytest.raises(UnknownEventTypeError, match="未知事件类型"):
        SessionEvent.from_dict(data)


def test_unknown_event_with_ignorable_marker_is_accepted_but_skipped(tmp_path: Path):
    path = tmp_path / "_no-cwd_" / "s1" / "session.v1.jsonl"
    path.parent.mkdir(parents=True)
    header = SessionHeader(session_id="s1", created_at="2020-01-01T00:00:00+00:00")
    lines = [
        json.dumps(header.to_dict()),
        json.dumps({"seq": 1, "kind": "future/thing", "payload": {}, "at": "", "ignorable": True}),
        json.dumps({"seq": 2, "kind": TURN_START, "payload": {"turn": 1, "text": "hi"}, "at": "x"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    events, report = SessionReader(path).events()
    assert [event.kind for event in events] == [TURN_START]
    assert report.ignored_unknown == 1


def test_known_event_types_cover_everything_we_write():
    from plugins.base.sessions import (
        ASSISTANT,
        COMPACT_CHECKPOINT,
        STEP_END,
        STEP_START,
        SYSTEM_MESSAGE,
        TOOL_CALL,
        TOOL_RESULT,
        TURN_END,
        USER_MESSAGE,
    )

    expected = {
        SESSION_START, SYSTEM_MESSAGE, TURN_START, TURN_END, STEP_START,
        STEP_END, ASSISTANT, TOOL_CALL, TOOL_RESULT, COMPACT_CHECKPOINT, USER_MESSAGE,
    }
    assert expected == set(KNOWN_EVENT_TYPES)


# ---------------------------------------------------------------------------
# 撕裂尾巴
# ---------------------------------------------------------------------------


def test_truncate_torn_tail_removes_partial_line(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c"')
    removed = truncate_torn_tail(path)
    assert removed > 0
    assert path.read_bytes() == b'{"a": 1}\n{"b": 2}\n'
    # 已经完整：再调一次不动
    assert truncate_torn_tail(path) == 0


def test_middle_corruption_is_not_silently_skipped(tmp_path: Path):
    path = tmp_path / "_no-cwd_" / "s1" / "session.v1.jsonl"
    path.parent.mkdir(parents=True)
    header = SessionHeader(session_id="s1", created_at="x")
    path.write_text(
        "\n".join(
            [
                json.dumps(header.to_dict()),
                json.dumps({"seq": 1, "kind": TURN_START, "payload": {"text": "a"}, "at": "x"}),
                "{not json at all",
                json.dumps({"seq": 3, "kind": TURN_START, "payload": {"text": "c"}, "at": "x"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="损坏"):
        SessionReader(path).events()


# ---------------------------------------------------------------------------
# 读写句柄 / flush
# ---------------------------------------------------------------------------


def test_writer_header_is_durable_immediately(tmp_path: Path):
    """header 必须在写句柄创建时落盘——`stat` / `list` 只读它。"""
    path = session_path(tmp_path, "s1")
    header = SessionHeader(session_id="s1", created_at="x")
    writer = SessionWriter(path, header)  # 不显式 flush
    try:
        # 句柄还活着（没被回收/关闭）时就必须能读到 header
        assert path.stat().st_size > 0
        assert SessionReader(path).header().session_id == "s1"
    finally:
        writer.close()


def test_writer_repairs_torn_tail_on_open(tmp_path: Path):
    path = session_path(tmp_path, "s1")
    header = SessionHeader(session_id="s1", created_at="x")
    writer = SessionWriter(path, header)
    writer.append(SessionEvent(1, TURN_START, {"turn": 1, "text": "a"}, "x"))
    writer.flush()
    writer.close()

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "bad')

    reopened = SessionWriter(path, header)
    assert reopened.repaired_bytes > 0
    reopened.append(SessionEvent(2, TURN_START, {"turn": 2, "text": "b"}, "x"))
    reopened.close()

    events, report = SessionReader(path).events()
    assert [event.seq for event in events] == [1, 2]
    assert report.truncated_bytes == 0


def test_writer_refuses_a_different_session_id(tmp_path: Path):
    path = session_path(tmp_path, "s1")
    SessionWriter(path, SessionHeader(session_id="s1", created_at="x")).close()
    with pytest.raises(SessionFormatError, match="会话 id 不匹配"):
        SessionWriter(path, SessionHeader(session_id="s2", created_at="x"))


def test_flush_is_a_global_barrier(tmp_path: Path):
    _ctx, sessions, store = booted(tmp_path)
    with ctx_session(sessions, "s1") as _s:
        sessions.append("s1", TURN_START, {"turn": 1, "text": "a"})
    sessions.open("s2")
    sessions.append("s2", TURN_START, {"turn": 1, "text": "b"})

    # 两个会话都有打开的写句柄；一次 flush 覆盖它们
    store.flush()
    for session_id in ("s1", "s2"):
        events, _report = SessionReader(store.path_for(session_id)).events()
        assert events, f"{session_id} 没落盘"


class ctx_session:
    """小上下文：打开一条会话并在退出时不做额外动作（只为可读性）。"""

    def __init__(self, sessions, session_id: str) -> None:
        self._sessions = sessions
        self._session_id = session_id

    def __enter__(self):
        return self._sessions.open(self._session_id)

    def __exit__(self, *_exc: object) -> None:
        return None
