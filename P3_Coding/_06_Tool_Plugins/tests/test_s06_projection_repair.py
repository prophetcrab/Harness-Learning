"""投影的修剪（`sanitize_pairs`）：**日志被截断时，会话不该永久卡死**。

**现场（用户报告）**：一个会话里每轮请求都失败，页面上只有一行
`出错：turn 执行失败：Error code: 400 - Messages with role 'tool' must be a
response to a preceding message with 'tool_calls'`。

成因：会话日志**头部被截**（文件被清理过/被外部改过），投影出的第一条消息变成
某个工具结果，而它对应的 `assistant(tool_calls)` 已经不在了。这种历史**每次**
都会被 API 拒绝——于是"这条会话再也发不出任何请求"，用户完全不知道该怎么办。

修法：投影最后做一遍**只删不造**的修剪（孤儿结果丢掉、没人应答的调用摘掉）。
日志一个字节不动；重放仍然同构（每次重放做同样的修剪）。
"""

from __future__ import annotations

from components.session.events import Event
from components.session.projection import derive_messages, sanitize_pairs


def _assistant(seq: int, content: str, *call_ids: str, turn: int = 1, step: int = 1) -> Event:
    return Event(
        seq=seq,
        type="assistant/message",
        data={
            "turn": turn, "step": step, "content": content,
            "tool_calls": [
                {"id": call_id, "name": "shell", "arguments": {"command": "x"}}
                for call_id in call_ids
            ],
        },
    )


def _result(seq: int, call_id: str, result: dict, *, turn: int = 1, step: int = 1) -> Event:
    return Event(
        seq=seq,
        type="tool/result",
        data={
            "turn": turn, "step": step, "call_id": call_id, "name": "shell",
            "result": result, "is_error": False,
        },
    )


def _user(seq: int, text: str, *, turn: int = 1) -> Event:
    return Event(seq=seq, type="user/message", data={"content": text, "turn": turn})


# ---------------------------------------------------------------------------
# 正常日志：修剪是 no-op
# ---------------------------------------------------------------------------


def test_healthy_log_is_untouched():
    """配对完好的历史**一条都不动**（修剪只在坏日志上生效）。"""
    events = [
        _user(1, "跑一下"),
        _assistant(2, "好", "c1"),
        _result(3, "c1", {"ok": True}),
        _user(4, "再来"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[1].tool_calls[0].id == "c1"
    assert messages[2].tool_call_id == "c1"


def test_multiple_calls_in_one_assistant_message_are_kept():
    """一次申请多个调用、结果分条跟随：全都保留（正常形态，别误伤）。"""
    events = [
        _user(1, "两件事"),
        _assistant(2, "我并行跑", "c1", "c2"),
        _result(3, "c1", {"a": 1}),
        _result(4, "c2", {"b": 2}),
    ]
    roles = [m.role for m in derive_messages(events)]
    assert roles == ["user", "assistant", "tool", "tool"]


# ---------------------------------------------------------------------------
# 坏日志（1）：孤儿工具结果（头部被截）
# ---------------------------------------------------------------------------


def test_orphan_tool_result_is_dropped():
    """**头部被截** → 孤儿结果丢掉，会话恢复可用。"""
    events = [
        _result(104, "call_gone", {"ok": True}),          # 它的申请已不在日志里
        _user(150, "算一下"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user"], (
        "孤儿工具结果没被丢掉——API 会拒绝这份历史，这条会话就废了"
    )


def test_orphan_results_in_the_middle_are_dropped_but_good_ones_survive():
    events = [
        _result(104, "call_gone", {"ok": True}),          # 孤儿
        _user(105, "接着说"),
        _assistant(106, "跑", "c1"),
        _result(107, "c1", {"ok": True}),                 # 有主
        _result(108, "call_other", {"ok": True}),         # 孤儿（另一个不存在的申请）
        _user(109, "再说"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2].tool_call_id == "c1"


# ---------------------------------------------------------------------------
# 坏日志（2）：调用了但没有结果（进程在结果落盘前被杀）
# ---------------------------------------------------------------------------


def test_unanswered_call_is_trimmed_from_the_assistant_message():
    """assistant 声明了调用但结果没落盘 → 把那个调用摘掉（整条消息保住）。"""
    events = [
        _user(1, "跑"),
        _assistant(2, "我来", "c1", "c2"),
        _result(3, "c1", {"ok": True}),                   # c2 的结果丢了
        _user(4, "继续"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "user"]
    assert [call.id for call in messages[1].tool_calls] == ["c1"], (
        "没人应答的调用还挂在历史里——API 会拒绝（每个 tool_call 都必须有结果）"
    )
    assert messages[1].content == "我来"                  # 正文保住


def test_assistant_message_left_empty_by_trimming_is_dropped():
    """摘空且没有正文 → 整条丢掉（留一条空的 assistant 也是噪声）。"""
    events = [
        _user(1, "跑"),
        _assistant(2, "", "c1"),                          # 只有这个调用，结果没落盘
        _user(3, "继续"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user", "user"]


def test_trailing_unanswered_call_is_also_trimmed():
    """日志的**最后**一条就是没结果的调用（刚被杀的现场）也要摘。

    （正文留着：模型说过"开始"，那句话是事实；摘掉的只是那个没人应答的调用。）
    """
    events = [
        _user(1, "跑"),
        _assistant(2, "开始", "c1"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "开始"
    assert messages[1].tool_calls == [], "收尾时没摘掉那个没人应答的调用"


# ---------------------------------------------------------------------------
# 与折叠（compact/checkpoint）共存
# ---------------------------------------------------------------------------


def test_sanitize_runs_after_folding():
    """折叠之后再做修剪：折叠可能把一条 assistant 折走，留下它的结果。"""
    events = [
        _user(1, "很早的问题"),
        _result(2, "call_gone", {"ok": True}),            # 孤儿（在折叠区里）
        Event(seq=3, type="compact/checkpoint",
              data={"summary": "（前面聊过一段）", "folded": 2}),
        _user(4, "新的问题"),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["user", "user"]
    assert "前面聊过一段" in messages[0].content


def test_sanitize_pairs_is_pure_and_repeatable():
    """同样的输入跑两次得到同样的结果（重放同构：不是随机/有状态的修剪）。"""
    events = [
        _result(1, "call_gone", {"ok": True}),
        _user(2, "问"),
        _assistant(3, "", "c1"),
    ]
    first = [m.role for m in derive_messages(events)]
    second = [m.role for m in derive_messages(events)]
    assert first == second == ["user"]


def test_sanitize_pairs_can_be_used_directly():
    """公开的函数（诊断/别的投影入口可用）；传空列表也安全。"""
    assert sanitize_pairs([]) == []
