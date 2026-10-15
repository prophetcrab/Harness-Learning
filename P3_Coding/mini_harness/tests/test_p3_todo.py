"""`_06` 任务清单测试：三条约束 / 整表折叠 / 模型工具 / 人命令 / 端到端。

验收标准（PLAN.md `_06_Tasks`）：**跨 turn 任务清单持久；投影一致；resume 后还能看到**。
本文件把这三句各写成一簇硬断言。

与 goal（若做）的分工：todo 是"接下来做哪几步"（执行步骤，模型自己拆），
不是"做完是什么样"（完成条件）。所以它**不驱动自动续跑**——本文件也钉住这一点。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from components.session.events import Event
from components.session.log import Session
from components.todo import (
    MAX_IN_PROGRESS,
    MAX_TODOS,
    TodoError,
    TodoItem,
    TodoStore,
    fold_todos,
    format_todos,
    normalize,
    run_todo_command,
    summarize,
)
from plugins import TODOS_SERVICE, WEBUI_SERVICE, boot_offline, webui_plugin


def _store() -> tuple[TodoStore, Session]:
    session = Session("t", store=None)
    return TodoStore(append=session.append, load_events=lambda: session.events), session


def _boot(tmp_path: Path, *, script: list | None = None, **kwargs):
    return boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=script if script is not None else [text_reply("好")],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 条目与约束
# ---------------------------------------------------------------------------


def test_item_round_trips():
    item = TodoItem(content="写代码", status="in_progress")
    assert TodoItem.from_dict(item.to_dict()) == item


def test_item_rejects_empty_content():
    with pytest.raises(TodoError, match="不能为空"):
        TodoItem.from_dict({"content": "   "})
    with pytest.raises(TodoError):
        TodoItem.from_dict({"content": "x", "status": "进行中"})   # 状态非法


def test_normalize_trims_and_keeps_order():
    items = normalize([{"content": "  第一  "}, {"content": "第二", "status": "completed"}])
    assert [i.content for i in items] == ["第一", "第二"]
    assert items[0].status == "pending"          # 缺省 pending


def test_normalize_rejects_duplicates():
    with pytest.raises(TodoError, match="重复"):
        normalize([{"content": "同一件事"}, {"content": "同一件事"}])


def test_normalize_allows_only_one_in_progress():
    """**同时最多一条 in_progress**——一次做一件事，这是纪律不是限制。"""
    with pytest.raises(TodoError, match="一次做一件事"):
        normalize([
            {"content": "甲", "status": "in_progress"},
            {"content": "乙", "status": "in_progress"},
        ])
    # 一条没问题
    assert len(normalize([{"content": "甲", "status": "in_progress"}])) == 1
    # 多条 completed 也没问题（历史记录）
    assert len(normalize([{"content": "甲", "status": "completed"},
                          {"content": "乙", "status": "completed"}])) == 2


def test_normalize_caps_list_size():
    with pytest.raises(TodoError, match="最多"):
        normalize([{"content": f"第 {i} 条"} for i in range(MAX_TODOS + 1)])


def test_max_in_progress_constant_is_one():
    assert MAX_IN_PROGRESS == 1


# ---------------------------------------------------------------------------
# 整表替换与折叠
# ---------------------------------------------------------------------------


def test_write_replaces_whole_list():
    """每次写入整表替换——**不是增量**（对齐 dsh 的 todo_write）。"""
    store, _ = _store()
    store.write([{"content": "甲"}, {"content": "乙"}])
    store.write([{"content": "丙"}])              # 整表替换
    assert [i.content for i in store.current()] == ["丙"]


def test_fold_is_last_write_wins():
    store, session = _store()
    store.write([{"content": "第一版"}])
    store.write([{"content": "第二版"}])
    assert [i.content for i in fold_todos(session.events)] == ["第二版"]


def test_fold_returns_none_before_first_write():
    """从未写过 = None（区别于空清单）——界面据此判断"要不要显示这块"。"""
    _, session = _store()
    assert fold_todos(session.events) is None


def test_empty_list_is_a_valid_write():
    """空清单是合法的（= 清空），与"从未写过"不是一回事。"""
    store, session = _store()
    store.write([{"content": "甲"}])
    store.clear()
    assert store.current() == []
    assert fold_todos(session.events) == []


def test_fold_rejects_corrupt_log():
    """坏负载在折叠时当场报错——不静默折出一张假清单。"""
    session = Session("t", store=None)
    session.append("todo/write", {"todos": [{"content": "甲", "status": "in_progress"},
                                            {"content": "乙", "status": "in_progress"}]})
    with pytest.raises(TodoError):
        fold_todos(session.events)


def test_fold_tolerates_missing_status():
    """状态可缺省（老版本/手写日志）：缺省 pending，不算坏数据。"""
    session = Session("t", store=None)
    session.append("todo/write", {"todos": [{"content": "只有内容"}]})
    assert fold_todos(session.events)[0].status == "pending"


def test_event_type_registered():
    from components.session.events import EVENT_TYPES, validate

    assert "todo/write" in EVENT_TYPES
    validate(Event(seq=1, type="todo/write", data={"todos": []}))
    with pytest.raises(ValueError):
        validate(Event(seq=1, type="todo/write", data={}))


# ---------------------------------------------------------------------------
# store 的便利方法（都只是读-改-写，日志里永远只有整表快照）
# ---------------------------------------------------------------------------


def test_convenience_methods_all_produce_whole_snapshots():
    store, session = _store()
    store.add("甲")
    store.add("乙")
    store.set_status("甲", "in_progress")
    store.set_status("甲", "completed")
    assert [i.status for i in store.current()] == ["completed", "pending"]
    # 每一次操作都写了一条完整的 todo/write（没有"增量"事件）
    writes = [e for e in session.events if e.type == "todo/write"]
    assert len(writes) == 4
    assert all("todos" in e.data for e in writes)


def test_set_status_unknown_content_fails_loud():
    store, _ = _store()
    store.add("甲")
    with pytest.raises(ValueError, match="没有这一条"):
        store.set_status("不存在", "completed")


def test_summarize_shows_progress():
    assert summarize(None) == "还没有任务清单。"
    assert summarize([]) == "清单为空（全部清掉了）。"
    todos = normalize([{"content": "甲", "status": "completed"},
                       {"content": "乙", "status": "in_progress"}])
    assert "1/2 已完成" in summarize(todos) and "乙" in summarize(todos)


# ---------------------------------------------------------------------------
# 人命令（不花模型的钱）
# ---------------------------------------------------------------------------


def test_todo_command_full_flow():
    store, _ = _store()
    assert "还没有任务清单" in run_todo_command(store, "").text
    assert run_todo_command(store, "读需求").changed            # 直接写内容
    assert run_todo_command(store, "写代码").changed
    assert run_todo_command(store, "跑测试").changed
    assert "3" in format_todos(store.current())                 # 带序号

    # 用序号勾选（人看得到第几条，敲数字比抄句子快）
    assert run_todo_command(store, "done 1").changed
    assert store.current()[0].status == "completed"
    assert run_todo_command(store, "doing 2").changed
    assert store.current()[1].status == "in_progress"
    assert run_todo_command(store, "todo 2").changed            # 退回待办
    assert store.current()[1].status == "pending"
    assert run_todo_command(store, "rm 3").changed
    assert len(store.current()) == 2
    assert run_todo_command(store, "clear").changed
    assert store.current() == []


def test_todo_command_accepts_content_substring():
    """记不住全句也能勾：唯一包含匹配即可（多条匹配要人用序号）。"""
    store, _ = _store()
    run_todo_command(store, "把文档里所有失败的检查项修好")
    assert run_todo_command(store, "done 文档").changed
    assert store.current()[0].status == "completed"


def test_todo_command_rejects_ambiguous_substring():
    store, _ = _store()
    run_todo_command(store, "修文档甲")
    run_todo_command(store, "修文档乙")
    result = run_todo_command(store, "done 修文档")
    assert not result.changed and "匹配到多条" in result.text


def test_todo_command_accepts_chinese_aliases():
    store, _ = _store()
    assert run_todo_command(store, "加 甲").changed
    assert run_todo_command(store, "完成 1").changed
    assert store.current()[0].status == "completed"
    assert run_todo_command(store, "在做 1").changed
    assert run_todo_command(store, "待办 1").changed
    assert run_todo_command(store, "删除 1").changed
    assert store.current() == []


def test_todo_command_never_raises():
    store, _ = _store()
    assert "用法" in run_todo_command(store, "add").text          # 少了内容
    # 空清单时先回"清单是空的"（比"序号越界"更切题，是刻意的顺序）
    assert "清单是空的" in run_todo_command(store, "done 9").text
    run_todo_command(store, "甲")
    assert "超出范围" in run_todo_command(store, "done 9").text   # 有清单了才判越界
    # 一次做一件事的约束也变成人话
    run_todo_command(store, "乙")
    run_todo_command(store, "doing 1")
    result = run_todo_command(store, "doing 2")
    assert not result.changed and "一次做一件事" in result.text


# ---------------------------------------------------------------------------
# 模型工具 + 会话归属
# ---------------------------------------------------------------------------


def test_model_writes_list_to_running_session(tmp_path: Path):
    """模型在 g1 里写清单，就该进 g1 的日志（工具是全局注册的）。"""
    script = [
        tool_call_reply("todo_write", {"todos": [{"content": "第一步"}]}),
        text_reply("列好了。"),
    ]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("帮我规划", session_id="g1")
    todos = booted.ctx.require(TODOS_SERVICE)
    assert [i.content for i in todos.current("g1")] == ["第一步"]
    assert todos.current("default") is None, "不该跑到 default 去"
    booted.close()


def test_model_tool_rejects_bad_list_with_error_not_crash(tmp_path: Path):
    """约束不满足时工具返回 {"error": ...}，不抛异常炸掉 turn。"""
    script = [
        tool_call_reply("todo_write", {"todos": [{"content": "甲", "status": "in_progress"},
                                                 {"content": "乙", "status": "in_progress"}]}),
        text_reply("知道了。"),
    ]
    booted = _boot(tmp_path, script=script)
    result = booted.agent.ask("随便", session_id="s1")
    assert result.status == "done"                      # turn 正常结束
    errors = [tr for step in result.steps for tr in step.tool_results if tr.error]
    assert errors and "一次做一件事" in str(errors[0].result)
    assert booted.ctx.require(TODOS_SERVICE).current("s1") is None   # 坏清单没进日志
    booted.close()


def test_description_tells_model_when_to_use_it_proactively(tmp_path: Path):
    """工具描述要**主动教模型什么时候用**，而不只讲格式。

    背景（实测）：描述里原来只写"多步骤的活儿先列计划"，模型于是基本不会自发
    调用——用户说"帮我做三件事"（没提"计划"），模型直接闷头做，清单一条没有；
    只有明说"先列个计划"才会用。补上"用户在要两件以上的事 → 开工前先写清单"
    这条**触发条件**（并列出反例"单步小事别建"）之后，描述才真正起到引导作用。

    注意：这条测试钉的是**描述里有这条指引**，不是"模型一定会照做"——后者是模型
    行为，随模型/提示词变化，不该写成硬断言（真有需要时得靠更强的机制，如强制
    前置步骤或界面提示）。
    """
    from plugins.todo import TODO_WRITE_DESCRIPTION as desc

    assert "什么时候该主动用" in desc                     # 有触发条件这一节
    assert "两件以上的事" in desc                          # 给出可识别的信号
    assert "别闷头一件件做" in desc                        # 说明不用的后果
    assert "单步的小事别建" in desc                        # 同时给出反例，防滥用
    assert "整表替换" in desc                              # 格式约束仍在
    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    booted.close()


def test_empty_write_from_model_is_clear(tmp_path: Path):
    script = [
        tool_call_reply("todo_write", {"todos": [{"content": "甲"}]}),
        text_reply("记下了。"),
        tool_call_reply("todo_write", {"todos": []}),
        text_reply("清掉了。"),
    ]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("第一轮", session_id="s1")
    assert booted.ctx.require(TODOS_SERVICE).current("s1")
    booted.agent.ask("第二轮", session_id="s1")
    assert booted.ctx.require(TODOS_SERVICE).current("s1") == []
    booted.close()


# ---------------------------------------------------------------------------
# 端到端：跨 turn / 投影一致 / resume 后还在
# ---------------------------------------------------------------------------


def test_list_survives_turns_and_resume(tmp_path: Path):
    """PLAN 的验收三连：跨 turn 持久、投影一致、resume 后还在。"""
    script = [
        tool_call_reply("todo_write", {"todos": [
            {"content": "读需求", "status": "completed"},
            {"content": "写代码", "status": "in_progress"},
            {"content": "跑测试", "status": "pending"},
        ]}),
        text_reply("计划列好了。"),
    ]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("帮我做这个功能", session_id="t1")
    first = [i.to_dict() for i in booted.ctx.require(TODOS_SERVICE).current("t1")]
    assert len(first) == 3
    booted.close()

    reopened = _boot(tmp_path)
    again = [i.to_dict() for i in reopened.ctx.require(TODOS_SERVICE).current("t1")]
    assert again == first, "resume 后的清单应与重启前逐字相同"
    # 而且还能继续改（先把在做的那条勾掉，再开下一条——一次做一件事）
    store = reopened.ctx.require(TODOS_SERVICE).store("t1")
    store.set_status("写代码", "completed")
    store.set_status("跑测试", "in_progress")
    assert store.current()[2].status == "in_progress"
    reopened.close()


def test_projection_matches_live_state(tmp_path: Path):
    """投影一致：由日志现折出的清单 == 服务读到的清单。"""
    script = [tool_call_reply("todo_write", {"todos": [{"content": "甲"}]}),
              text_reply("好。")]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("规划", session_id="t1")
    events = booted.agent.open("t1").session.events
    folded = [i.to_dict() for i in fold_todos(events)]
    live = [i.to_dict() for i in booted.ctx.require(TODOS_SERVICE).current("t1")]
    assert folded == live
    booted.close()


def test_list_does_not_drive_continuation(tmp_path: Path):
    """**清单不驱动续跑**——它是"我想怎么干"，不是"你要求我干到什么时候"。

    （如果清单也能续跑，模型随手列个十步计划就等于签了十轮支票；
    持续推进的职责归 `goal` 那种机制。）
    """
    script = [tool_call_reply("todo_write", {"todos": [{"content": "甲"}, {"content": "乙"}]}),
              text_reply("列好了。")]
    booted = _boot(tmp_path, script=script)
    result = booted.agent.ask("规划一下", session_id="t1")
    events = booted.agent.open("t1").session.events
    assert sum(1 for e in events if e.type == "turn/start") == 1, "清单不该触发额外轮次"
    assert result.status == "done"
    booted.close()


def test_one_session_instance_per_id(tmp_path: Path):
    """**同名会话只有一个 Session 对象**——两个消费者必须看到同一份活日志。

    回归背景：`open()` 起初每次新建 Session，agent 与 todo 存储各拿一份内存
    副本，一方 append 的事件另一方在内存里永远看不到（磁盘上其实写了），
    于是清单读不出来。
    """
    booted = _boot(tmp_path)
    sessions = booted.ctx.require("sessions")
    first, _, _ = sessions.open("same")
    second, _, _ = sessions.open("same")
    assert first is second
    first.append("user/message", {"content": "hello"})
    assert any(e.data.get("content") == "hello" for e in sessions.load("same")[0])
    booted.close()


# ---------------------------------------------------------------------------
# 插件回卷与界面接线
# ---------------------------------------------------------------------------


def test_plugin_rolls_back(tmp_path: Path):
    booted = _boot(tmp_path)
    assert TODOS_SERVICE in booted.ctx.services
    assert "todo_write" in booted.ctx.require("tools").names

    booted.ctx.unload_plugin("todo")
    assert TODOS_SERVICE not in booted.ctx.services
    assert "todo_write" not in booted.ctx.require("tools").names
    booted.close()


def test_no_plugin_means_no_tool(tmp_path: Path):
    booted = _boot(tmp_path)
    booted.ctx.unload_plugin("todo")
    assert "todo_write" not in booted.ctx.require("tools").names
    result = booted.agent.ask("随便说说", session_id="s1")
    assert result.status == "done"          # 一切照旧
    booted.close()


def test_webui_exposes_todo_view_and_command(tmp_path: Path):
    """网页侧：命令菜单有 /todo；GET 看清单、POST 执行命令（都不花模型的钱）。"""
    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    # 命令菜单里有 /todo
    with urllib.request.urlopen(url + "app.js", timeout=8) as response:
        js = response.read().decode("utf-8")
    assert 'name: "/todo"' in js
    with urllib.request.urlopen(url, timeout=8) as response:
        html = response.read().decode("utf-8")
    assert 'id="todo-box"' in html and 'id="banner"' in html   # 清单面板 + 提示条

    # 空清单
    with urllib.request.urlopen(url + "api/sessions/w1/todo", timeout=8) as response:
        assert json.load(response)["todos"] is None

    # POST 一条命令（直接写内容 = 追加）
    request = urllib.request.Request(
        url + "api/sessions/w1/todo", data=json.dumps({"line": "跑一遍测试"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        data = json.load(response)
    assert data["changed"] and [t["content"] for t in data["todos"]] == ["跑一遍测试"]

    # GET 再读一次，能看到刚写的
    with urllib.request.urlopen(url + "api/sessions/w1/todo", timeout=8) as response:
        assert [t["content"] for t in json.load(response)["todos"]] == ["跑一遍测试"]
    booted.close()


def test_webui_todo_route_503_without_plugin(tmp_path: Path):
    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    booted.ctx.unload_plugin("todo")
    try:
        urllib.request.urlopen(url + "api/sessions/w1/todo", timeout=8)
        raise AssertionError("本该 503")
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
    booted.close()


__all__ = ["MAX_IN_PROGRESS"]
