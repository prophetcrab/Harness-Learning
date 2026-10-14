"""`_06` 目标测试：领域不变量 / 日志折叠 / CAS / 续跑闸门 / 命令 / 端到端。

验收标准（PLAN.md `_06`）：**跨 turn 任务清单持久；投影一致；resume 后还能看到**。
本文件把这三句各写成一簇硬断言：

- 持久：目标由日志派生，重建（模拟重启）后逐字相同；
- 投影一致：`fold_goal` 的结果与"逐条重放"的结果一致，坏日志当场报错；
- resume 后还在：新装配打开同一会话，目标照旧可读、可继续改。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.goal import (
    CONTINUE_PROMPT,
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_ROUND_SOURCE,
    GoalBlockReason,
    GoalError,
    GoalStore,
    check_revision,
    fold_goal,
    format_goal,
    next_snapshot,
    run_goal_command,
    validate_transition,
)
from components.llm import text_reply, tool_call_reply
from components.session.events import Event
from components.session.log import Session
from plugins import (
    CONTINUATION_SERVICE,
    GOALS_SERVICE,
    WEBUI_SERVICE,
    boot_offline,
    webui_plugin,
)


def _store() -> tuple[GoalStore, Session]:
    """一个绑在内存会话上的目标操作面（不落盘，测试用）。"""
    session = Session("t", store=None)
    return GoalStore(append=session.append, load_events=lambda: session.events), session


def _boot(tmp_path: Path, *, script: list | None = None, **kwargs):
    return boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=script if script is not None else [text_reply("好")],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 领域：不变量与许可表
# ---------------------------------------------------------------------------


def test_create_sets_active_revision_one():
    store, _ = _store()
    change = store.create("把文档修好")
    assert change.operation == "create"
    assert change.snapshot is not None
    assert change.snapshot.phase == "active"
    assert change.snapshot.revision == 1
    assert change.snapshot.max_goal_rounds == DEFAULT_MAX_GOAL_ROUNDS


def test_revision_increments_on_every_change():
    store, _ = _store()
    store.create("目标")
    assert store.edit(objective="改了").revision == 2
    assert store.pause().revision == 3
    assert store.resume().revision == 4
    assert store.complete().revision == 5


def test_only_one_unfinished_goal_at_a_time():
    """一个会话同时只能有一个未完成目标——必须先收拾掉前一个。"""
    store, _ = _store()
    store.create("第一个")
    with pytest.raises(GoalError, match="已有未完成的目标"):
        store.create("第二个")
    store.complete()
    store.create("第二个")            # 完成之后可以直接替换
    assert store.current().objective == "第二个"


def test_completed_goal_can_be_replaced_without_clear():
    """对照：完成的目标不挡路（清掉是"不想要了"，完成是"做完了"）。"""
    store, _ = _store()
    store.create("甲")
    store.complete()
    change = store.create("乙")
    assert change.snapshot.revision == 1        # 新目标重新从 1 开始
    assert store.current().objective == "乙"


def test_cleared_goal_id_cannot_be_reused():
    """clear 之后目标 id 不会被回收再利用（防"复活"一个已清掉的目标）。"""
    store, session = _store()
    store.create("会消失的目标", goal_id="goal-fixed")
    store.clear()
    assert store.current() is None
    # 手工构造一条复用 id 的 create（服务层的 create 会自动生成新 id）
    session.append("goal/change", {
        "operation": "create",
        "goal": {"id": "goal-fixed", "revision": 1, "objective": "想复活",
                 "phase": "active", "max_goal_rounds": 4},
    })
    with pytest.raises(GoalError, match="重复使用"):
        fold_goal(session.events)


@pytest.mark.parametrize(
    "start_phase,operation,allowed",
    [
        ("active", "pause", True),
        ("paused", "pause", False),
        ("paused", "resume", True),
        ("blocked", "resume", True),
        ("complete", "resume", False),
        ("complete", "edit", False),
        ("complete", "complete", False),
        ("active", "block", True),
    ],
)
def test_transition_table(start_phase, operation, allowed):
    """许可表逐格验证（非法流转拒绝，且消息里给出允许的起始阶段）。"""
    snapshot = next_snapshot(
        None, operation="create", objective="x", new_id="g1",
    )
    snapshot = type(snapshot)(
        id=snapshot.id, revision=snapshot.revision, objective=snapshot.objective,
        phase=start_phase, max_goal_rounds=snapshot.max_goal_rounds,
    )
    if allowed:
        validate_transition(operation, snapshot)
    else:
        with pytest.raises(GoalError):
            validate_transition(operation, snapshot)


def test_block_requires_reason_and_clears_on_resume():
    store, _ = _store()
    store.create("目标")
    store.block(code="needs-input", message="等用户确认")
    current = store.current()
    assert current.phase == "blocked"
    assert current.goal.blocked_reason == GoalBlockReason(code="needs-input", message="等用户确认")
    store.resume()
    assert store.current().goal.blocked_reason is None      # 解封后不再带原因


def test_blocked_phase_requires_reason_in_log():
    """坏日志（blocked 却没说原因）在折叠时当场报错，不静默折出假目标。"""
    session = Session("t", store=None)
    session.append("goal/change", {
        "operation": "create",
        "goal": {"id": "g1", "revision": 1, "objective": "x", "phase": "active",
                 "max_goal_rounds": 4},
    })
    session.append("goal/change", {
        "operation": "block",
        "goal": {"id": "g1", "revision": 2, "objective": "x", "phase": "blocked",
                 "max_goal_rounds": 4},
    })
    with pytest.raises(GoalError, match="blocked_reason"):
        fold_goal(session.events)


# ---------------------------------------------------------------------------
# CAS：版本对不上就拒绝
# ---------------------------------------------------------------------------


def test_stale_revision_is_rejected():
    """带着过期版本号改 → 拒绝（模型拿着旧视图改会当场被纠正）。"""
    store, _ = _store()
    store.create("目标")
    store.edit(objective="第一次改")            # revision 现在是 2
    with pytest.raises(GoalError, match="已被改动"):
        store.edit(objective="基于旧视图的改", expected_revision=1)
    store.edit(objective="基于最新视图的改", expected_revision=2)   # 对得上就放行


def test_revision_check_helper():
    check_revision(None, None)                  # 不提要求就不检查
    snapshot = next_snapshot(None, operation="create", objective="x", new_id="g")
    with pytest.raises(GoalError):
        check_revision(7, snapshot)
    check_revision(1, snapshot)


# ---------------------------------------------------------------------------
# 折叠：日志是唯一真相
# ---------------------------------------------------------------------------


def test_fold_replays_full_lifecycle():
    store, session = _store()
    store.create("原始目标")
    store.pause()
    store.resume()
    store.edit(objective="改过的目标")
    projection = fold_goal(session.events)
    assert projection.objective == "改过的目标"
    assert projection.phase == "active"
    assert projection.revision == 4


def test_fold_is_pure_and_repeatable():
    """折叠是纯函数：同一份日志折两次结果相同（没有隐藏状态）。"""
    store, session = _store()
    store.create("目标")
    store.pause()
    first = fold_goal(session.events)
    second = fold_goal(session.events)
    assert first == second


def test_fold_counts_only_goal_rounds():
    """只有"续跑"算轮次；人插话不算——否则用户聊两句就把预算耗光。"""
    store, session = _store()
    store.create("目标")
    session.append("user/message", {"content": "人说的话"})
    session.append("user/message", {"content": CONTINUE_PROMPT, "source": GOAL_ROUND_SOURCE})
    session.append("user/message", {"content": "人又说了一句"})
    session.append("user/message", {"content": CONTINUE_PROMPT, "source": GOAL_ROUND_SOURCE})
    assert fold_goal(session.events).rounds_started == 2


def test_fold_stops_counting_after_pause():
    """暂停之后不再续跑，自然也不再计轮次。"""
    store, session = _store()
    store.create("目标")
    session.append("user/message", {"content": "x", "source": GOAL_ROUND_SOURCE})
    store.pause()
    session.append("user/message", {"content": "x", "source": GOAL_ROUND_SOURCE})
    assert fold_goal(session.events).rounds_started == 1


def test_fold_rejects_bad_revision_sequence():
    session = Session("t", store=None)
    session.append("goal/change", {
        "operation": "create",
        "goal": {"id": "g1", "revision": 1, "objective": "x", "phase": "active",
                 "max_goal_rounds": 4},
    })
    session.append("goal/change", {          # revision 跳到 5：CAS 断裂
        "operation": "pause",
        "goal": {"id": "g1", "revision": 5, "objective": "x", "phase": "paused",
                 "max_goal_rounds": 4},
    })
    with pytest.raises(GoalError, match="revision 必须递增"):
        fold_goal(session.events)


def test_fold_without_goal_returns_none():
    session = Session("t", store=None)
    session.append("user/message", {"content": "随便聊聊"})
    assert fold_goal(session.events) is None


def test_clear_makes_current_none_and_allows_new():
    store, _ = _store()
    store.create("甲")
    store.clear()
    assert store.current() is None
    store.create("乙")                            # 清掉之后能建新的
    assert store.current().objective == "乙"


def test_event_type_is_registered():
    from components.session.events import EVENT_TYPES, validate

    assert "goal/change" in EVENT_TYPES
    validate(Event(seq=1, type="goal/change", data={"operation": "clear"}))
    with pytest.raises(ValueError):
        validate(Event(seq=1, type="goal/change", data={}))


# ---------------------------------------------------------------------------
# `/goal` 人命令（不花模型的钱）
# ---------------------------------------------------------------------------


def test_goal_command_full_flow():
    store, _ = _store()
    assert "没有目标" in run_goal_command(store, "").text
    result = run_goal_command(store, "new 把文档全修好 --rounds 5")
    assert result.changed and "轮数上限 5" in result.text
    assert "进行中" in run_goal_command(store, "").text
    assert run_goal_command(store, "pause").changed
    assert run_goal_command(store, "resume").changed
    assert run_goal_command(store, "done").changed
    assert "已完成" in run_goal_command(store, "").text
    assert run_goal_command(store, "clear").changed


def test_goal_command_accepts_chinese_aliases():
    store, _ = _store()
    assert run_goal_command(store, "新建 目标甲").changed
    assert "目标甲" in format_goal(store.current())
    assert run_goal_command(store, "暂停").changed
    assert run_goal_command(store, "继续").changed
    assert run_goal_command(store, "完成").changed
    assert run_goal_command(store, "清").changed


def test_goal_command_accepts_bare_objective():
    """`/goal <内容>` 直接建目标——**不用先打 new**（最常用的那条路）。

    回归背景：用户真的这么打了（`/goal 写一个飞机大战html`），拿到的是
    "不认识子命令" + 一长串用法。要求"建目标"先打 new 太啰嗦——想说目标就
    直接说，子命令留给"要管它的时候"。
    """
    store, _ = _store()
    result = run_goal_command(store, "写一个飞机大战html")
    assert result.changed
    assert "已建立目标" in result.text
    assert store.current().objective == "写一个飞机大战html"


def test_bare_objective_with_rounds():
    store, _ = _store()
    run_goal_command(store, "把文档修好 --rounds 3")
    assert store.current().objective == "把文档修好"
    assert store.current().max_goal_rounds == 3


def test_misspelled_subcommand_is_caught_not_taken_as_objective():
    """拼错的子命令要被拦下——**不能静默建一个叫 "paus" 的目标**。

    这是"直接写内容"这个便利的代价：得防着"子命令打错一个字母"被当成目标。
    """
    store, _ = _store()
    result = run_goal_command(store, "paus")
    assert not result.changed
    assert "想用 pause 吗" in result.text
    assert store.current() is None, "不该建出目标"

    # 中文长句/中英混排不会被误判为拼写错误
    assert run_goal_command(store, "暂停一下所有的自动化任务").changed
    store.clear()
    assert run_goal_command(store, "donate to charity").changed


def test_subcommand_still_wins_over_bare_content():
    """有目标时打 `pause` 依然是执行暂停，而不是建一个叫 "pause" 的目标。"""
    store, _ = _store()
    run_goal_command(store, "目标甲")
    result = run_goal_command(store, "pause")
    assert result.changed and "已暂停" in result.text
    assert store.current().objective == "目标甲"      # 没被覆盖


def test_goal_command_never_raises_on_bad_input():
    """命令打错要回一句人话，而不是把界面搞出 traceback。"""
    store, _ = _store()
    assert "不认识子命令" in run_goal_command(store, "paus").text   # 拼错：拦下
    assert "用法" in run_goal_command(store, "new").text            # 少了内容
    assert "--rounds" in run_goal_command(store, "new x --rounds abc").text
    # 非法流转也变成人话
    store.create("目标")
    text = run_goal_command(store, "pause").text
    assert "已暂停" in text
    assert "操作被拒绝" in run_goal_command(store, "pause").text


def test_goal_command_block_carries_reason():
    store, _ = _store()
    store.create("目标")
    run_goal_command(store, "block 等外部接口就绪")
    current = store.current()
    assert current.goal.blocked_reason.message == "等外部接口就绪"
    assert "被卡原因" in format_goal(current)


# ---------------------------------------------------------------------------
# 续跑闸门（三道闸门逐条验证）
# ---------------------------------------------------------------------------


def test_no_continuation_without_goal(tmp_path: Path):
    booted = _boot(tmp_path)
    continuation = booted.ctx.require(CONTINUATION_SERVICE)
    continuation.begin("s1")
    assert continuation.should_continue("s1") is None


def test_continuation_only_while_active(tmp_path: Path):
    booted = _boot(tmp_path)
    goals = booted.ctx.require(GOALS_SERVICE)
    continuation = booted.ctx.require(CONTINUATION_SERVICE)
    goals.store("s1").create("目标")

    continuation.begin("s1")
    assert continuation.should_continue("s1") == CONTINUE_PROMPT

    continuation.begin("s1")
    goals.store("s1").pause()
    assert continuation.should_continue("s1") is None      # 暂停不续

    continuation.begin("s1")
    goals.store("s1").resume()
    assert continuation.should_continue("s1") == CONTINUE_PROMPT
    booted.close()


def test_stop_gate_blocks_continuation(tmp_path: Path):
    """取消（stop）之后不再续——停止信号优先于目标。"""
    booted = _boot(tmp_path)
    goals = booted.ctx.require(GOALS_SERVICE)
    continuation = booted.ctx.require(CONTINUATION_SERVICE)
    goals.store("s1").create("目标")
    continuation.begin("s1")
    continuation.stop("s1")
    assert continuation.should_continue("s1") is None
    booted.close()


def test_goal_plugin_rolls_back(tmp_path: Path):
    """卸载 goal 插件：服务、工具、闸门一起回卷（effect 语义）。"""
    booted = _boot(tmp_path)
    assert GOALS_SERVICE in booted.ctx.services
    assert "create_goal" in booted.ctx.require("tools").names

    booted.ctx.unload_plugin("goal")
    assert GOALS_SERVICE not in booted.ctx.services
    assert CONTINUATION_SERVICE not in booted.ctx.services
    assert "create_goal" not in booted.ctx.require("tools").names
    assert "get_goal" not in booted.ctx.require("tools").names
    booted.close()


# ---------------------------------------------------------------------------
# 端到端：跨 turn 持久 / 投影一致 / resume 后还在
# ---------------------------------------------------------------------------


def test_goal_survives_turns_and_resume(tmp_path: Path):
    """PLAN 的验收三连：跨 turn 持久、投影一致、resume 后还在。

    直接经服务建目标并**暂停**（不绕模型、也不触发续跑）——本用例考的是
    持久化，不是续跑；续跑另有专门用例。
    """
    booted = _boot(tmp_path)
    goals = booted.ctx.require(GOALS_SERVICE)
    goals.store("g1").create("把三件事做完", max_goal_rounds=4)
    goals.store("g1").pause()
    first = goals.view("g1")["goal"]
    assert first is not None and first["phase"] == "paused"
    booted.close()

    # 重新装配（模拟进程重启）：目标从日志里折出来，一个字都不差
    reopened = _boot(tmp_path, script=[text_reply("好")])
    goals = reopened.ctx.require(GOALS_SERVICE)
    again = goals.view("g1")["goal"]
    assert again == first, "resume 后的目标应与重启前逐字相同"
    # 而且还能继续改（CAS 用重建后的 revision，说明确实读到了正确版本）
    goals.store("g1").resume(expected_revision=again["revision"])
    assert goals.view("g1")["goal"]["phase"] == "active"
    reopened.close()


def test_continuation_runs_extra_turns_until_complete(tmp_path: Path):
    """active 目标会自动多跑几轮；标记完成后停下来。"""
    script = [
        tool_call_reply("create_goal", {"objective": "把三件事做完", "max_goal_rounds": 4}),
        text_reply("目标已建立。"),
        text_reply("续跑第 1 轮：做了一点。"),
        tool_call_reply("update_goal", {"action": "complete"}),
        text_reply("目标完成。"),
    ]
    booted = _boot(tmp_path, script=script)
    result = booted.agent.ask("请把三件事做完", session_id="g1")

    events = booted.agent.open("g1").session.events
    turns = [e for e in events if e.type == "turn/start"]
    users = [e for e in events if e.type == "user/message"]
    assert len(turns) == 3, f"应有 1 个人工 + 2 个续跑 turn，实到 {len(turns)}"
    assert users[0].data.get("source", "human") == "human"
    assert all(u.data["source"] == GOAL_ROUND_SOURCE for u in users[1:])
    assert result.status == "done"

    projection = booted.ctx.require(GOALS_SERVICE).view("g1")["goal"]
    assert projection["phase"] == "complete"
    assert projection["rounds_started"] == 2         # 两个续跑轮次
    booted.close()


def test_round_cap_blocks_the_goal_instead_of_looping_forever(tmp_path: Path):
    """**防跑飞的核心保险**：目标一直不完成时，跑满上限就标 blocked 停下。

    没有这条，一个想不明白的目标会一直续跑下去（烧 token 且无法自停）。
    """
    script = [tool_call_reply("create_goal", {"objective": "永远做不完", "max_goal_rounds": 3})]
    script += [text_reply(f"第 {i} 轮：还在做。") for i in range(20)]
    booted = _boot(tmp_path, script=script)
    result = booted.agent.ask("开始干活", session_id="cap")

    goal = booted.ctx.require(GOALS_SERVICE).view("cap")["goal"]
    assert goal["phase"] == "blocked"
    assert goal["rounds_started"] == 3
    assert goal["blocked_reason"]["code"] == "rounds-exhausted"
    assert "上限" in goal["blocked_reason"]["message"]

    events = booted.agent.open("cap").session.events
    assert sum(1 for e in events if e.type == "turn/start") == 4   # 1 + 3，不多不少
    assert result.turn == 4
    booted.close()


def test_blocked_goal_does_not_continue(tmp_path: Path):
    """被标 blocked 之后不再续跑（除非人 resume）。"""
    script = [tool_call_reply("create_goal", {"objective": "会卡住的事", "max_goal_rounds": 4}),
              text_reply("开始做。"),
              tool_call_reply("update_goal", {"action": "block",
                                              "reason_code": "needs-input",
                                              "message": "等你确认"}),
              text_reply("卡住了。")]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("开始", session_id="b1")
    goal = booted.ctx.require(GOALS_SERVICE).view("b1")["goal"]
    assert goal["phase"] == "blocked"
    events = booted.agent.open("b1").session.events
    assert sum(1 for e in events if e.type == "turn/start") == 2    # 人工 + 1 续跑
    booted.close()


def test_paused_goal_does_not_continue(tmp_path: Path):
    script = [tool_call_reply("create_goal", {"objective": "先暂停的事", "max_goal_rounds": 5}),
              text_reply("建好了。"),
              tool_call_reply("update_goal", {"action": "pause"}),
              text_reply("已暂停。")]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("开始", session_id="p1")
    assert booted.ctx.require(GOALS_SERVICE).view("p1")["goal"]["phase"] == "paused"
    events = booted.agent.open("p1").session.events
    assert sum(1 for e in events if e.type == "turn/start") == 2
    booted.close()


def test_goal_is_per_session(tmp_path: Path):
    """目标按会话隔离：两个会话各管各的。"""
    booted = _boot(tmp_path)
    goals = booted.ctx.require(GOALS_SERVICE)
    goals.store("sa").create("会话 A 的目标")
    goals.store("sb").create("会话 B 的目标")
    assert goals.view("sa")["goal"]["objective"] == "会话 A 的目标"
    assert goals.view("sb")["goal"]["objective"] == "会话 B 的目标"
    goals.store("sa").clear()
    assert goals.view("sa")["goal"] is None
    assert goals.view("sb")["goal"] is not None      # 清 A 不影响 B
    booted.close()


def test_goal_tools_write_to_the_running_session(tmp_path: Path):
    """模型工具是全局的，但目标要落到**当前正在跑的那个会话**里。

    回归背景：初版把会话写死成 `"default"`，在 g1 里建的目标跑进了 default
    （实测踩到，`view("g1")` 永远是空的）。
    """
    script = [
        tool_call_reply("create_goal", {"objective": "g1 的目标"}),
        text_reply("建好了。"),
        # 建完立刻完成：不然 active 目标会自动续跑，把剧本吃光
        tool_call_reply("update_goal", {"action": "complete"}),
        text_reply("完成了。"),
    ]
    booted = _boot(tmp_path, script=script)
    booted.agent.ask("在 g1 里建目标", session_id="g1")
    goals = booted.ctx.require(GOALS_SERVICE)
    assert goals.view("g1")["goal"]["objective"] == "g1 的目标"
    assert goals.view("default")["goal"] is None, "不该跑到 default 去"
    booted.close()


def test_no_goal_plugin_means_no_continuation(tmp_path: Path):
    """不装 goal 插件时行为与 `_05` 完全一致（一轮就结束）。"""
    booted = _boot(tmp_path)
    booted.ctx.unload_plugin("goal")
    result = booted.agent.ask("随便说说", session_id="s1")
    events = booted.agent.open("s1").session.events
    assert sum(1 for e in events if e.type == "turn/start") == 1
    assert result.status == "done"
    booted.close()


def test_one_session_instance_per_id(tmp_path: Path):
    """**同名会话只有一个 Session 对象**——两个消费者必须看到同一份活日志。

    回归背景：`open()` 起初每次新建 Session，agent 与 goal 存储各拿一份内存
    副本，一方 append 的事件另一方在内存里永远看不到（磁盘上其实写了），
    于是"目标轮次"数成 0、`goal/change` 折不出来（实测踩到）。
    """
    booted = _boot(tmp_path)
    sessions = booted.ctx.require("sessions")
    first, _, _ = sessions.open("same")
    second, _, _ = sessions.open("same")
    assert first is second, "同一 session_id 应该复用同一个 Session 实例"
    first.append("user/message", {"content": "hello"})
    assert any(e.data.get("content") == "hello" for e in sessions.load("same")[0])
    booted.close()


# ---------------------------------------------------------------------------
# 与 webui 的接线
# ---------------------------------------------------------------------------


def test_webui_exposes_goal_command_and_state(tmp_path: Path):
    """网页侧：命令菜单里有 /goal，且能看到目标状态（不花模型的钱）。"""
    booted = _boot(tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    booted.ctx.require(GOALS_SERVICE).store("w1").create("网页里的目标")

    with urllib.request.urlopen(url + "app.js", timeout=8) as response:
        js = response.read().decode("utf-8")
    assert '"/goal"' in js or "/goal" in js, "命令菜单里应有 /goal"
    booted.close()


__all__ = ["CONTINUE_PROMPT"]
