"""_04_Session_Log 的验收测试：单元级、全离线、不需要 key。

覆盖学习计划 M3 的验收 + _04 的新能力：
1. 投影：derive_messages 的映射与配对（system/user/assistant/tool）
2. 日志：append-only、seq 单调、深拷冻结（进/出都不被外部改坏）、校验 fail loud
3. 落盘：append → load 往返、list_sessions
4. **崩溃尾部修复**：半行 JSON 被自动截断，已确认事件不丢
5. **同构断言**：重放日志得到的消息历史 == 在线产生的历史
6. resume：重开会话后历史完整、turn 编号接续、新 turn 能继续
7. fork：分叉出前缀日志、原日志不动、目标已存在则报错
8. system 提示进日志：system_prompt 写成 session/start，投影出 system 消息

运行：cd P1_Coding/_04_Session_Log && python -m pytest -q
"""

import json

import pytest

from agent_loop import AgentLoop
from llm_seam import FakeLLM, build_default_toolbox, text_reply, tool_call_reply
from runner import Runner, fork_session, load_messages, open_session
from session import Event, JsonlStore, Session, derive_messages, validate


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def plain(messages):
    """把消息列表压成可比较的纯结构，便于断言与打印差异。"""
    out = []
    for m in messages:
        out.append(
            {
                "role": m.role,
                "content": m.content,
                "tool_calls": [(c.id, c.name, c.arguments) for c in m.tool_calls],
                "tool_call_id": m.tool_call_id,
            }
        )
    return out


def make_runner(session, provider, toolbox, *, max_steps=8):
    return Runner(session, provider, toolbox.execute, toolbox.specs(), max_steps=max_steps)


# =========================================================================
# 1) 投影
# =========================================================================


def test_projection_maps_events_to_messages():
    events = [
        Event(1, "session/start", {"session_id": "s", "system_prompt": "你是助手"}),
        Event(2, "turn/start", {"turn": 1, "user": "1+1"}),
        Event(3, "user/message", {"content": "1+1"}),
        Event(4, "step/start", {"turn": 1, "step": 1}),
        Event(
            5,
            "assistant/message",
            {"content": "", "tool_calls": [{"id": "c1", "name": "calculate", "arguments": {"expression": "1+1"}}]},
        ),
        Event(6, "tool/result", {"call_id": "c1", "name": "calculate", "result": {"result": 2}}),
        Event(7, "step/end", {"turn": 1, "step": 1}),
        Event(8, "turn/end", {"turn": 1, "status": "done"}),
    ]
    messages = derive_messages(events)
    assert [m.role for m in messages] == ["system", "user", "assistant", "tool"]
    tool_message = messages[3]
    assert tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content) == {"result": 2}


# =========================================================================
# 2) 日志纪律：seq / 冻结 / 校验
# =========================================================================


def test_append_assigns_monotonic_seq():
    session = Session("s")
    for i in range(1, 4):
        event = session.append("user/message", {"content": f"msg{i}"})
        assert event.seq == i
    assert session.last_seq == 3
    assert [e.seq for e in session.events] == [1, 2, 3]


def test_log_is_deep_copy_frozen():
    """进日志的负载被深拷贝：外部改原字典不影响日志；读出的也是拷贝。"""
    session = Session("s")
    caller_dict = {"content": "原始"}
    session.append("user/message", caller_dict)

    caller_dict["content"] = "被外部改了"        # 改调用方的字典
    assert session.events[0].data["content"] == "原始"  # 日志不受影响

    got = session.events[0]
    got.data["content"] = "改读出来的"            # 改读出来的事件
    assert session.events[0].data["content"] == "原始"  # 日志仍不受影响


def test_validation_is_loud():
    with pytest.raises(ValueError, match="未知事件类型"):
        validate(Event(1, "nope/type", {}))
    with pytest.raises(ValueError, match="缺少必填字段"):
        validate(Event(1, "tool/result", {"name": "x"}))  # 缺 call_id / result
    with pytest.raises(ValueError, match="tool_call 缺少字段"):
        validate(Event(1, "assistant/message", {"tool_calls": [{"name": "x"}]}))

    session = Session("s")
    with pytest.raises(ValueError):
        session.append("bad/type", {})  # 写入前校验，内存与磁盘都不会被污染
    assert session.last_seq == 0


# =========================================================================
# 3) 落盘往返 + list
# =========================================================================


def test_store_append_load_roundtrip(tmp_path):
    store = JsonlStore(tmp_path)
    session = Session("abc", store=store)
    session.append("session/start", {"session_id": "abc", "system_prompt": "hi"})
    session.append("user/message", {"content": "你好"})

    events, report = store.load("abc")
    assert not report.repaired
    assert [e.type for e in events] == ["session/start", "user/message"]
    assert events[1].data["content"] == "你好"
    assert "abc" in store.list_sessions()


# =========================================================================
# 4) 崩溃尾部修复
# =========================================================================


def test_truncated_tail_is_repaired(tmp_path):
    """尾部半行 JSON（模拟 kill -9）在 load 时被截断，已确认事件不丢。"""
    store = JsonlStore(tmp_path)
    session = Session("crash", store=store)
    session.append("session/start", {"session_id": "crash", "system_prompt": "hi"})
    session.append("user/message", {"content": "第一条"})

    path = store.path("crash")
    good = path.read_bytes()
    # 半截写入：一条"看起来要写但没写完"的事件（无结尾换行）
    path.write_bytes(good + b'{"seq": 3, "ts": "2026-01-01T00:00:00", "type": "user/mess')

    events, report = store.load("crash")
    assert report.repaired is True
    assert report.dropped_bytes > 0
    assert [e.type for e in events] == ["session/start", "user/message"]  # 前两条保住了
    # 文件已被就地截断修复，再次 load 不再报告修复
    events2, report2 = store.load("crash")
    assert report2.repaired is False
    assert len(events2) == 2


def test_truncated_tail_with_newline_is_repaired(tmp_path):
    """坏行含换行（写了半截 JSON 又恰好换行）也被丢弃。"""
    store = JsonlStore(tmp_path)
    session = Session("crash2", store=store)
    session.append("user/message", {"content": "ok"})
    path = store.path("crash2")
    path.write_bytes(path.read_bytes() + b'{"seq": 2, "type": broken\n')

    events, report = store.load("crash2")
    assert report.repaired is True
    assert [e.type for e in events] == ["user/message"]


# =========================================================================
# 5) 同构断言（本练习的核心验收）
# =========================================================================


def test_online_history_equals_replayed_history(tmp_path):
    """重放日志得到的消息历史 == 在线执行产生的历史。"""
    store = JsonlStore(tmp_path)
    toolbox = build_default_toolbox()
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("结果是 70066.52"),
        ]
    )
    session, _ = open_session("iso", store, system_prompt="你是计算助手")
    runner = make_runner(session, provider, toolbox)
    runner.send("帮我算 1234*56.78")

    online = plain(runner.history)          # 主循环内存里的历史（在线真相）
    replayed = plain(load_messages("iso", store))  # 从磁盘日志重放投影

    assert online == replayed
    assert [m for m in online if m["role"] == "tool"][0]["tool_call_id"] == "call_1"


# =========================================================================
# 6) resume：重开会话继续
# =========================================================================


def test_resume_continues_history_and_turn_number(tmp_path):
    store = JsonlStore(tmp_path)
    toolbox = build_default_toolbox()

    # 第一个进程：turn 1
    provider1 = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1+1"}),
            text_reply("等于 2"),
        ]
    )
    session1, report = open_session("res", store, system_prompt="你是助手")
    assert not report.repaired
    runner1 = make_runner(session1, provider1, toolbox)
    first = runner1.send("1+1")
    assert first.turn == 1
    provider1.assert_all_consumed()

    # "新进程"：重新打开同一会话，历史与 turn 编号都应延续
    provider2 = FakeLLM([text_reply("不客气！")])
    session2, _ = open_session("res", store, system_prompt="你是助手")  # system 不会再写一遍
    runner2 = make_runner(session2, provider2, toolbox)
    # resume 后的历史 = 上一轮的完整历史
    assert plain(runner2.history) == plain(runner1.history)

    second = runner2.send("谢谢")
    assert second.turn == 2  # turn 编号接续
    roles = [m.role for m in runner2.history]
    assert roles.count("system") == 1  # system 只注入一次
    assert roles[-2:] == ["user", "assistant"]
    # 重放也应该与在线一致
    assert plain(runner2.history) == plain(load_messages("res", store))


def test_resume_after_crash_repairs_then_continues(tmp_path):
    """崩溃留下坏尾 → resume 时自动修复 → 还能继续对话。"""
    store = JsonlStore(tmp_path)
    toolbox = build_default_toolbox()
    provider = FakeLLM([text_reply("第一轮回答")])

    session, _ = open_session("crash3", store, system_prompt="你是助手")
    make_runner(session, provider, toolbox).send("第一问")

    # 模拟崩溃：追加半行
    path = store.path("crash3")
    path.write_bytes(path.read_bytes() + b'{"seq": 999, "type": "user/mess')

    session2, report = open_session("crash3", store, system_prompt="你是助手")
    assert report.repaired is True

    provider2 = FakeLLM([text_reply("第二轮回答")])
    result = make_runner(session2, provider2, toolbox).send("第二问")
    assert result.turn == 2
    assert plain(session2.derive_messages()) == plain(load_messages("crash3", store))


# =========================================================================
# 7) fork
# =========================================================================


def test_fork_copies_prefix_and_leaves_original(tmp_path):
    store = JsonlStore(tmp_path)
    session = Session("orig", store=store)
    session.append("session/start", {"session_id": "orig", "system_prompt": ""})
    session.append("user/message", {"content": "A"})
    session.append("assistant/message", {"content": "回复 A"})
    session.append("user/message", {"content": "B"})

    forked = fork_session(store, "orig", "copy", upto_seq=3)  # 只到第 3 条

    assert [e.type for e in forked.events] == [
        "session/start",
        "user/message",
        "assistant/message",
    ]
    # 原会话不受影响（仍 4 条）
    orig_events, _ = store.load("orig")
    assert len(orig_events) == 4
    # 分叉会话已落盘
    assert "copy" in store.list_sessions()


def test_fork_refuses_existing_target(tmp_path):
    store = JsonlStore(tmp_path)
    Session("a", store=store).append("user/message", {"content": "x"})
    Session("b", store=store).append("user/message", {"content": "y"})
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        fork_session(store, "a", "b")


# =========================================================================
# 8) system 提示进日志
# =========================================================================


def test_system_prompt_lives_in_log(tmp_path):
    store = JsonlStore(tmp_path)
    session, _ = open_session("sys", store, system_prompt="这是系统提示")
    first_event = session.events[0]
    assert first_event.type == "session/start"
    assert first_event.data["system_prompt"] == "这是系统提示"
    assert session.derive_messages()[0].content == "这是系统提示"

    # 再次打开不应重复写 session/start
    session2, _ = open_session("sys", store, system_prompt="这是系统提示")
    starts = [e for e in session2.events if e.type == "session/start"]
    assert len(starts) == 1
