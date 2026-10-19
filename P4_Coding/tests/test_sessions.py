"""sessions + session-store-jsonl 验收：事件 / 投影 / 落盘 / 恢复 / 修复。

事件形状（组件层的约定）：

    session/start → turn/start → assistant → tool/call + tool/result → turn/end
"""

from __future__ import annotations

from pathlib import Path

from core import Context
from plugins.base.sessions import (
    ASSISTANT,
    SESSION_START,
    SESSIONS_SERVICE,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_END,
    TURN_START,
    SessionEvent,
    project_messages,
    sessions_plugin,
)
from plugins.capability.session.store_jsonl import (
    SESSION_STORE_SERVICE,
    session_store_jsonl_plugin,
)


def booted(tmp_path: Path) -> tuple[Context, object, object]:
    ctx = Context()
    ctx.load_plugin(sessions_plugin())
    ctx.load_plugin(session_store_jsonl_plugin(root=tmp_path))
    return ctx, ctx.require(SESSIONS_SERVICE), ctx.require(SESSION_STORE_SERVICE)


# ---------------------------------------------------------------------------
# 会话内核
# ---------------------------------------------------------------------------


def test_open_creates_session_with_start_event(tmp_path):
    _ctx, sessions, _store = booted(tmp_path)
    session, resumed = sessions.open("s1", system_prompt="你是助手")
    assert resumed is False
    assert session.events[0].kind == SESSION_START
    assert session.effective_system_prompt() == "你是助手"


def test_open_twice_returns_same_session(tmp_path):
    _ctx, sessions, _store = booted(tmp_path)
    first, _ = sessions.open("s1")
    second, resumed = sessions.open("s1")
    assert second is first and resumed is True


def test_append_assigns_increasing_seq_and_broadcasts(tmp_path):
    ctx, sessions, _store = booted(tmp_path)
    sessions.open("s1")
    seen: list[tuple[str, int]] = []
    ctx.on(
        "session/event",
        lambda session_id, session_event: seen.append((session_id, session_event["seq"])),
    )

    sessions.append("s1", TURN_START, {"turn": 1, "text": "你好"})
    sessions.append("s1", TURN_END, {"turn": 1, "status": "done", "final_text": "你好"})
    assert [seq for _, seq in seen] == [2, 3]
    assert all(sid == "s1" for sid, _ in seen)


def test_system_message_shadows_start_prompt(tmp_path):
    _ctx, sessions, _store = booted(tmp_path)
    sessions.open("s1", system_prompt="旧提示词")
    sessions.append("s1", "system/message", {"content": "新提示词"})
    assert sessions.system_prompt("s1") == "新提示词"


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------


def test_projection_builds_message_history(tmp_path):
    _ctx, sessions, _store = booted(tmp_path)
    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "帮我算 1+1"})
    sessions.append(
        "s1",
        ASSISTANT,
        {
            "content": "我来算",
            "tool_calls": [{"id": "c1", "name": "calc", "arguments": '{"expr":"1+1"}'}],
        },
    )
    sessions.append("s1", TOOL_CALL, {"call_id": "c1", "name": "calc"})
    sessions.append("s1", TOOL_RESULT, {"call_id": "c1", "status": "ok", "content": {"value": 2}})
    sessions.append("s1", ASSISTANT, {"content": "等于 2"})
    sessions.append("s1", TURN_END, {"turn": 1, "status": "done", "final_text": "等于 2"})

    messages = sessions.messages("s1")
    assert [(m.role, m.content) for m in messages] == [
        ("user", "帮我算 1+1"),
        ("assistant", "我来算"),
        ("tool", '{"value": 2}'),
        ("assistant", "等于 2"),
    ]
    assert messages[1].tool_calls[0].name == "calc"
    assert messages[2].tool_call_id == "c1"


def test_projection_is_cached_until_next_event(tmp_path):
    _ctx, sessions, _store = booted(tmp_path)
    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "一"})
    first = sessions.messages("s1")
    assert sessions.messages("s1") == first  # 缓存

    sessions.append("s1", TURN_START, {"turn": 2, "text": "二"})
    assert len(sessions.messages("s1")) == 2  # 事件变了，缓存失效


def test_projection_is_a_pure_function():
    events = [
        SessionEvent(1, TURN_START, {"turn": 1, "text": "hi"}, ""),
        SessionEvent(2, ASSISTANT, {"content": "hello"}, ""),
    ]
    messages = project_messages(events)
    assert [(m.role, m.content) for m in messages] == [("user", "hi"), ("assistant", "hello")]


# ---------------------------------------------------------------------------
# 落盘 / 恢复
# ---------------------------------------------------------------------------


def test_events_are_persisted_and_restored(tmp_path: Path):
    ctx, sessions, store = booted(tmp_path)
    sessions.open("s1", system_prompt="你是助手")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "你好"})
    sessions.append("s1", ASSISTANT, {"content": "你好呀"})

    store.flush()  # 耐久屏障（turn/end 也会自动触发）
    assert store.path_for("s1").exists()
    assert "s1" in store.session_ids()
    assert store.stat("s1")["session_id"] == "s1"

    # 换一个"进程"：新 Context，同一个目录
    ctx2, sessions2, _store2 = booted(tmp_path)
    session, resumed = sessions2.open("s1")
    assert resumed is True
    assert session.effective_system_prompt() == "你是助手"
    assert [(m.role, m.content) for m in sessions2.messages("s1")] == [
        ("user", "你好"),
        ("assistant", "你好呀"),
    ]


def test_truncated_last_line_is_repaired(tmp_path: Path):
    _ctx, sessions, store = booted(tmp_path)
    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "正常"})

    store.flush()
    # 模拟崩溃：往文件末尾追加半行
    path = store.path_for("s1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 99, "kind": "assistant", "payl')

    ctx2, sessions2, store2 = booted(tmp_path)
    session, resumed = sessions2.open("s1")
    assert resumed is True
    assert [e.kind for e in session.events] == [SESSION_START, TURN_START]
    assert store2.last_repair is not None
    assert store2.last_repair.truncated_bytes > 0
    assert store2.last_repair.repaired is True

    # 写打开会**截掉**坏字节（不让文件一直带着坏尾巴）
    sessions2.append("s1", TURN_START, {"turn": 2, "text": "恢复后继续"})
    store2.flush()
    raw = path.read_text(encoding="utf-8")
    assert '"seq": 99' not in raw   # 半行（seq 99）已被截掉
    assert raw.endswith("\n")


def test_unsafe_session_id_is_sanitized():
    from plugins.capability.session.store_jsonl import JsonlSessionStore

    root = Path("/tmp/x")
    store = JsonlSessionStore(root)
    path = store.path_for("../../etc/passwd")
    assert root in path.parents or path.parent == root   # 没有逃出根目录
    assert ".." not in path.name                          # 文件名里没有路径穿越
    assert "/" not in path.name and "\\" not in path.name


def test_session_id_is_encoded_injectively():
    """不同 id 必须不同名（单射）：`a/b` 与 `a_b` 不能撞同一个目录。"""
    from plugins.capability.session.store_jsonl import encode_session_id

    assert encode_session_id("a/b") != encode_session_id("a_b")
    assert "/" not in encode_session_id("a/b")


def test_store_snapshot_reports_root_and_sessions(tmp_path: Path):
    _ctx, sessions, store = booted(tmp_path)
    sessions.open("a")
    sessions.open("b")
    store.flush()
    snap = store.snapshot()
    assert snap["root"] == str(tmp_path)
    assert snap["sessions"] == ["a", "b"]
    assert snap["details"][0]["size_bytes"] > 0


# ---------------------------------------------------------------------------
# 不装持久化插件：内存照常工作
# ---------------------------------------------------------------------------


def test_memory_only_works_without_store():
    ctx = Context()
    ctx.load_plugin(sessions_plugin())
    sessions = ctx.require(SESSIONS_SERVICE)
    sessions.open("s1")
    sessions.append("s1", TURN_START, {"turn": 1, "text": "你好"})
    assert len(sessions.messages("s1")) == 1  # 不落盘，但会话能用
    assert ctx.require(SESSIONS_SERVICE).session_ids == ("s1",)


def test_unloading_store_stops_persistence(tmp_path: Path):
    ctx, sessions, store = booted(tmp_path)
    sessions.open("s1")
    ctx.unload_plugin("session-store-jsonl")
    assert ctx.get(SESSION_STORE_SERVICE) is None

    sessions.append("s1", TURN_START, {"turn": 1, "text": "卸载后"})
    # 文件里只有 header + session/start（之后的没落盘）
    lines = store.path_for("s1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
