"""`_05` 上下文压缩测试：触发 / 切点 / 摘要 / 日志不改写 / 重放同构 / fail-soft。

验收标准（PLAN.md `_05`）：**压缩后投影变化、日志原样；重放仍然同构**。
本文件把这三句各写成一簇硬断言：

- 投影变化：`derive_messages` 里折叠区变成一条摘要；
- 日志原样：`user/message` / `assistant/message` / `tool/result` 一条没少、内容没改；
- 重放同构：把日志重新装载（模拟进程重启）后投影出的历史，与在线时的内存历史
  逐条相等——这是"打开即恢复"在压缩场景下的推广。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from components.agent import (
    CompactionPolicy,
    Compactor,
    choose_cut,
    plan_compaction,
)
from components.context.tokens import DEFAULT_CONTEXT_WINDOW, message_tokens
from components.llm import text_reply, tool_call_reply
from components.llm.vocabulary import ToolCall, assistant, system, tool_result, user
from components.session.projection import conversation_pairs, derive_messages
from plugins import (
    COMPACTER_SERVICE,
    WEBUI_SERVICE,
    CompactPlugin,
    boot_offline,
    compact_plugin,
    webui_plugin,
)

#: 小容量：触发线 2700、保留 300、摘要上限 150 —— 测试里几轮就能压。
SMALL = CompactionPolicy(capacity=3000)

BIG = "这是一段足够长的文本，用来把历史撑到触发线以上。" * 40


def _policy(**overrides) -> CompactionPolicy:
    base = {
        "capacity": 3000,
        "trigger_ratio": 0.9,
        "keep_ratio": 0.1,
        "summary_ratio": 0.05,
        "min_tail": 2,
    }
    base.update(overrides)
    return CompactionPolicy(**base)


def _conversation(turns: int):
    """构造一段假历史：[u, a] * turns（不含 system）。"""
    messages = []
    for index in range(turns):
        messages.append(user(f"第{index}问：" + BIG))
        messages.append(assistant(f"第{index}答：" + "回" * 100))
    return messages


# ---------------------------------------------------------------------------
# 策略：参数校验与三个比例
# ---------------------------------------------------------------------------


def test_policy_derives_thresholds_from_capacity():
    policy = _policy()
    assert policy.trigger_tokens == pytest.approx(2700)
    assert policy.keep_tokens == pytest.approx(300)
    assert policy.summary_token_limit == 150          # 3000 * 5%
    assert policy.transcript_char_budget == 2000      # 3000*0.4/0.6 = 2000


def test_policy_rejects_nonsense():
    with pytest.raises(ValueError):
        _policy(capacity=0)
    with pytest.raises(ValueError):
        _policy(trigger_ratio=0.0)
    with pytest.raises(ValueError):
        _policy(keep_ratio=0.95)      # 保留 >= 触发 = 压了立刻又超
    with pytest.raises(ValueError):
        _policy(summary_ratio=0.0)
    with pytest.raises(ValueError):
        _policy(min_tail=0)


def test_default_window_is_one_million():
    """`_05` 起默认窗口 = 1M（用户指定：从 64K 改为 1M）。"""
    assert DEFAULT_CONTEXT_WINDOW == 1_048_576


# ---------------------------------------------------------------------------
# 触发判断（纯函数）：什么时候压、压哪一段
# ---------------------------------------------------------------------------


def test_no_compaction_when_small():
    messages = [system("系统"), user("你好"), assistant("在的")]
    assert plan_compaction(messages, [], SMALL) is None


def test_triggers_over_threshold_and_folds_the_old_part():
    messages = [system("系统"), *_conversation(6)]
    plan = plan_compaction(messages, [], SMALL)
    assert plan is not None
    assert plan.start == 1                  # system 不参与折叠
    assert 1 < plan.cut < len(messages)     # 尾巴留着
    assert plan.folded == plan.cut - plan.start
    # 折叠区必须比保留区大（否则"压缩"名不副实）；尾巴另有 min_tail 硬下限，
    # 所以保留区允许略高于 keep 预算——那是有意的（保住最短的近期语境）。
    assert plan.folded_tokens > plan.kept_tokens


def test_measured_tokens_can_trigger_even_when_estimate_is_small():
    """实测（上一次响应的 prompt_tokens）优先于估算——它才代表模型真看到的量。"""
    messages = [system("系统")]
    for index in range(6):                     # 小消息：估算约 420，远不到触发线
        messages.append(user(f"第{index}问：" + "abcd" * 25))
        messages.append(assistant("ok " * 30))
    assert plan_compaction(messages, [], SMALL) is None
    plan = plan_compaction(messages, [], SMALL, measured=9000)
    assert plan is not None and plan.used == 9000 and plan.used_source == "actual"


def test_never_cuts_tool_call_apart_from_its_result():
    """切点不能落在 tool 消息上：那样申请被折掉、结果却留下，API 会直接拒绝。

    构造刻意让"按预算切的点"正好落在 tool 消息上（保留预算 900 恰好装下
    tool 结果 + 最终回答，装不下更早的工具申请）——没有成对保护时，
    plan.cut 会指向 messages[3]（tool）。
    """
    messages = [
        system("系统"),
        user(BIG),
        assistant("", [ToolCall(id="c1", name="read", arguments={"path": "长" * 200})]),
        tool_result("c1", "R" * 800),
        assistant("结论：" + BIG),
    ]
    plan = plan_compaction(messages, [], _policy(capacity=1500, keep_ratio=0.6, min_tail=1))
    assert plan is not None
    assert messages[plan.cut].role != "tool", "切点落在了 tool 消息上——成对保护失效"
    # 通用不变式：跨切点的工具申请/结果配对必须完整（两边同在折叠区或同在保留区）
    folded_ids = {call.id for m in messages[plan.start:plan.cut] for call in m.tool_calls}
    kept_ids = {m.tool_call_id for m in messages[plan.cut:] if m.tool_call_id}
    assert not (folded_ids & kept_ids)


def test_choose_cut_never_reaches_into_the_current_turn():
    """floor（本 turn 起点）及其之后的一切永不折叠：保留区起点必须钳在它之前。"""
    messages = [system("系统"), *_conversation(6)]
    floor = len(messages) - 2                     # 最后一个 turn 的两条
    cut = choose_cut(messages, 1, SMALL, floor=floor)
    assert cut <= floor, "折叠区够到了当前 turn（切点越过了 floor）"

    # 极端：整个历史都属于当前 turn（floor = start）→ 一条都不折
    assert choose_cut(messages, 1, SMALL, floor=1) == 1


def test_plan_is_none_when_only_current_turn_exists():
    messages = [system("系统"), user(BIG), assistant(BIG)]
    assert plan_compaction(messages, [], SMALL, protect_from=1) is None


# ---------------------------------------------------------------------------
# Compactor：摘要请求、摘要文本、结果对象
# ---------------------------------------------------------------------------


class _Recorder:
    """替身 provider：记下每次请求，返回固定回复。"""

    def __init__(self, reply: str = "备忘：用户在做 P3 练习。") -> None:
        self.requests = []
        self.reply = reply

    def complete(self, request):
        self.requests.append(request)
        return text_reply(self.reply)


def test_summary_request_is_plain_text_without_tools():
    """摘要请求必须"谁都能读"：没有 tools、消息摊平成文本（不带 tool 角色）。"""
    provider = _Recorder()
    compactor = Compactor(provider, SMALL)
    messages = [system("系统"), *_conversation(6)]
    outcome = compactor.maybe_compact(messages, [])

    assert outcome is not None
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.tools == []
    assert [m.role for m in request.messages] == ["system", "user"]
    assert "[tool]" not in request.messages[1].content     # 已摊平成文本
    assert "第0问" in request.messages[1].content          # 首条（原始目标）必带
    assert request.messages[1].content.count("\n\n") > 0   # 多条摊平在一起


def test_summary_result_shape_and_prefix():
    provider = _Recorder("要点一；要点二。")
    outcome = Compactor(provider, SMALL).maybe_compact([system("系统"), *_conversation(6)], [])
    assert outcome is not None
    assert outcome.message_content.startswith("【早前对话摘要】")
    assert "要点一；要点二。" in outcome.message_content
    assert outcome.summary_tokens > 0
    assert outcome.before_tokens >= 2700
    assert outcome.after_tokens < outcome.before_tokens   # 确实是"压缩"


def test_summary_over_limit_is_flagged_not_truncated():
    """摘要超长只标记，不裁剪——裁剪会悄悄丢信息；标记让上层能看见。"""
    provider = _Recorder("很长" * 500)
    outcome = Compactor(provider, SMALL).maybe_compact([system("系统"), *_conversation(6)], [])
    assert outcome is not None and outcome.over_limit is True


def test_transcript_keeps_head_and_marks_dropped_middle():
    from components.agent.compact import build_transcript

    messages = [user("第一条" * 50), assistant("中间" * 500), user("最后一条")]
    text = build_transcript(messages, per_message_chars=200, total_chars=210)
    assert "第一条" in text and "最后一条" in text
    assert "已略" in text


# ---------------------------------------------------------------------------
# 端到端：投影变化 + 日志原样 + 重放同构
# ---------------------------------------------------------------------------


def _boot(tmp_path: Path, *, turns: int, capacity: int = 3000):
    """装配一个可反复对话的 harness。

    刻意**不给严格剧本**（用 offline_llm_plugin 的固定回复）：压缩每次都会
    额外调用一次模型做摘要，用例关心的是"折叠了多少条、日志是否原样"，
    不该被剧本条数绑住。
    """
    return boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws", context_window=capacity
    )


def test_compaction_folds_projection_but_keeps_log(tmp_path: Path):
    booted = _boot(tmp_path, turns=6)
    opened = booted.agent.open("s1")
    for index in range(6):
        booted.agent.ask(f"第{index}问：" + BIG, session_id="s1")

    events = opened.session.events
    checkpoints = [e for e in events if e.type == "compact/checkpoint"]
    assert checkpoints, "6 轮大消息之后应该已经压过了"

    # ① 投影变化：投影里出现摘要，且不再有最初的 user 消息
    messages = opened.messages
    folded_text = "".join(m.content or "" for m in messages)
    assert "【早前对话摘要】" in folded_text
    assert "第0问" not in folded_text, "最早的 user 消息应已被折进摘要"

    # ② 日志原样：一条没少、内容没改（8 条 user 全在）
    user_events = [e for e in events if e.type == "user/message"]
    assert len(user_events) == 6
    assert any("第0问" in e.data["content"] for e in user_events), "被折掉的原文仍在日志里"
    # 旧事件没有被改写：6 轮 = 6 条助手消息，一条不少（压缩只追加 checkpoint）
    assert sum(1 for e in events if e.type == "assistant/message") == 6
    assert sum(1 for e in events if e.type == "turn/end") == 6
    # 且 seq 严格递增（append-only 的证据）
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    # ③ checkpoint 是"折叠声明"，自带账目
    data = checkpoints[0].data
    assert data["folded"] >= 1
    assert data["before_tokens"] >= 2700
    assert data["after_tokens"] < data["before_tokens"]
    booted.close()


def test_replay_after_restart_is_isomorphic(tmp_path: Path):
    """重放同构：把日志重新装载（新进程视角）后投影，与在线内存历史逐条相等。"""
    booted = _boot(tmp_path, turns=6)
    opened = booted.agent.open("s1")
    for index in range(6):
        booted.agent.ask(f"第{index}问：" + BIG, session_id="s1")
    online = [(m.role, m.content, [c.name for c in m.tool_calls]) for m in opened.loop.history]
    assert any("【早前对话摘要】" in (m[1] or "") for m in online)
    booted.close()

    # 重开一个装配（同一个 root）：resume 走"装载日志 → 投影"
    reopened = _boot(tmp_path, turns=6)
    resumed = reopened.agent.open("s1")
    assert resumed.resumed is True
    replayed = [(m.role, m.content, [c.name for c in m.tool_calls]) for m in resumed.loop.history]
    assert replayed == online, "重放出的历史与在线时不同构（压缩破坏了日志一致性）"
    reopened.close()


def test_second_compaction_swallows_the_first_summary(tmp_path: Path):
    """第二次压缩把第一版摘要一起吞掉——摘要不该链式堆积。"""
    booted = _boot(tmp_path, turns=8)
    opened = booted.agent.open("s1")
    for index in range(8):
        booted.agent.ask(f"第{index}问：" + BIG, session_id="s1")

    messages = opened.messages
    summaries = [m for m in messages if "【早前对话摘要】" in (m.content or "")]
    assert len(summaries) == 1, f"投影里应只剩一条摘要，实到 {len(summaries)} 条"
    checkpoints = [e for e in opened.session.events if e.type == "compact/checkpoint"]
    assert len(checkpoints) >= 2
    # 第二条 checkpoint 折掉的条数 >= 第一条写的摘要 + 它之后的尾巴：
    # 摘要确实被"一起吞进去"了（位置语义下就是折叠了列表最前那一条摘要）
    assert checkpoints[-1].data["folded"] >= 1
    booted.close()


def test_current_turn_is_never_folded(tmp_path: Path):
    """压缩不许碰"本 turn"的消息：刚问的问题、刚拿到的结果都得在。"""
    # 每轮：先算一次（带工具往返），让 turn 中间也在长
    script = []
    for _ in range(4):
        script += [tool_call_reply("calculate", {"expression": "1+1"}),
                   text_reply("算完了。" + "回" * 300)]
    booted = _boot_scripted(tmp_path, script, capacity=1500)
    opened = booted.agent.open("s1")
    for index in range(4):
        booted.agent.ask(f"第{index}问（很长的问题）：" + BIG, session_id="s1")
        # 每个 turn 结束后：本轮的 user 提问必须还在投影里（它是最新的，当然在）
        contents = "".join(m.content or "" for m in opened.messages)
        assert f"第{index}问" in contents
    # 而更早的轮次可以被折掉
    assert "第0问" not in "".join(m.content or "" for m in opened.messages)
    booted.close()


def test_checkpoint_counts_tool_pairs_consistently(tmp_path: Path):
    """带工具往返的会话：日志投影与内存历史必须逐条一致（含工具配对）。"""
    pair = [tool_call_reply("calculate", {"expression": "2*3"}),
            text_reply("答案是 6。" + "回" * 300)]
    booted = _boot_scripted(tmp_path, pair * 4, capacity=1200)
    opened = booted.agent.open("s1")
    for index in range(4):
        booted.agent.ask(f"第{index}问：" + BIG, session_id="s1")
    events = opened.session.events
    assert [e for e in events if e.type == "compact/checkpoint"]
    # 投影与内存历史一致（工具配对没被破坏）
    projected = [(m.role, m.content) for m in opened.messages]
    memory = [(m.role, m.content) for m in opened.loop.history]
    assert projected == memory
    # 折叠没有劈开任何工具配对：每个 tool 消息都能在列表里找到它的申请
    roles = [(m.role, m.tool_calls, m.tool_call_id) for m in opened.messages]
    pending: set = set()
    for role, calls, call_id in roles:
        for call in calls:
            pending.add(call.id)
        if role == "tool":
            assert call_id in pending, "出现了找不到申请的工具结果（配对被劈开了）"
    booted.close()


# ---------------------------------------------------------------------------
# 接缝：插件 / 服务 / 卸载
# ---------------------------------------------------------------------------


def test_compact_plugin_provides_service_and_unload_disables(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[text_reply("hi")], context_window=3000)
    assert COMPACTER_SERVICE in booted.ctx.services

    booted.ctx.unload_plugin("compact")
    assert COMPACTER_SERVICE not in booted.ctx.services
    # 卸载后再开会话：loop 的 compactor 是 None（压缩整个关掉）
    opened = booted.agent.open("no-compact")
    assert opened.loop._compactor is None
    booted.close()


def test_plugin_rejects_both_policy_and_capacity():
    with pytest.raises(ValueError):
        CompactPlugin(policy=SMALL, capacity=999)
    plugin = compact_plugin(capacity=3000)
    assert plugin.policy.capacity == 3000


def test_compaction_emits_kernel_event_for_observers(tmp_path: Path):
    """压缩发生时要发 `context/compacted`（界面靠它显示"压缩了"）。"""
    seen = []
    booted = _boot(tmp_path, turns=6)
    booted.ctx.on("context/compacted", lambda **payload: seen.append(payload))
    for index in range(6):
        booted.agent.ask(f"第{index}问：" + BIG, session_id="s1")
    assert seen, "没有收到 context/compacted 事件"
    applied = [item for item in seen if item["status"] == "applied"]
    assert applied and applied[0]["folded"] >= 1
    assert applied[0]["before_tokens"] > applied[0]["after_tokens"] > 0
    booted.close()


# ---------------------------------------------------------------------------
# fail-soft：摘要失败不能把对话搞死
# ---------------------------------------------------------------------------


class _SummaryAwareProvider:
    """剧本 provider + 摘要专用回复：按请求形态分流。

    压缩的摘要调用是"普通模型调用"，会走到同一个 provider——严格剧本会被
    它吃掉一条（甚至把 tool_call 剧本当摘要答复）。凡是"剧本 + 压缩"同时
    在场的用例都用这个替身：摘要请求固定回一句备忘，其余按剧本播放。
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.requests = []

    @staticmethod
    def _is_summary(request) -> bool:
        return (
            len(request.messages) == 2
            and request.messages[0].role == "system"
            and "对话压缩器" in (request.messages[0].content or "")
        )

    def complete(self, request):
        self.requests.append(request)
        if self._is_summary(request):
            return text_reply("备忘：既定目标与关键结论。" + "要" * 30)
        if not self._script:
            raise AssertionError("剧本用完（本用例的剧本条数不够）")
        return self._script.pop(0)


def _boot_scripted(tmp_path: Path, script: list, *, capacity: int):
    """装配 + 换成摘要感知的剧本 provider（换插件 = 换 provider，走 inject 重装）。"""
    from plugins import LlmPlugin

    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[], context_window=capacity)
    booted.ctx.unload_plugin("llm")
    booted.ctx.load_plugin(LlmPlugin(_SummaryAwareProvider(script)))
    return booted


class _BrokenCompactor:
    """总是炸的压缩器：验证 fail-soft 与"失败不进日志"。"""

    def maybe_compact(self, messages, tools, *, measured=None, protect_from=None):
        raise RuntimeError("摘要服务挂了")


def test_summary_failure_is_soft_and_not_logged(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[text_reply("答" + "回" * 100)], context_window=3000)
    # 换成会炸的压缩器（重装插件：CompactPlugin 的 setup 从 ctx 取 llm）
    booted.ctx.unload_plugin("compact")
    booted.ctx.load_plugin(
        type("Broken", (), {
            "name": "compact",
            "inject": ("llm",),
            "setup": lambda self, ctx: ctx.provide(COMPACTER_SERVICE, _BrokenCompactor()),
        })()
    )
    result = booted.agent.ask("问一句很长的：" + BIG, session_id="s1")
    assert result.status == "done"                      # 对话照常完成
    events = booted.agent.open("s1").session.events
    assert not [e for e in events if e.type == "compact/checkpoint"]   # 失败不落日志
    booted.close()


def test_failed_summary_then_next_turn_still_can_compact(tmp_path: Path):
    """一次失败不影响下一次：阈值还在，下次超了照样压（fail-soft 是"跳过"）。"""

    class _Flaky:
        """第一次摘要调用炸，之后正常。"""

        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("第一次摘要失败")
            return self.inner.complete(request)

    provider = _Flaky(_Recorder("事后补上的摘要。"))
    compactor = Compactor(provider, SMALL)
    messages = [system("系统"), *_conversation(6)]
    with pytest.raises(RuntimeError):
        compactor.maybe_compact(messages, [])
    second = compactor.maybe_compact(messages, [])
    assert second is not None and "事后补上的摘要" in second.summary


# ---------------------------------------------------------------------------
# 摘要进入请求的形态
# ---------------------------------------------------------------------------


def test_summary_message_role_is_user_and_carries_prefix():
    provider = _Recorder("要点。")
    outcome = Compactor(provider, SMALL).maybe_compact([system("系统"), *_conversation(6)], [])
    assert outcome is not None
    assert outcome.message_content.startswith("【早前对话摘要】")


def test_token_estimate_helper_used_by_both_sides():
    """压缩与仪表共用同一个估算函数（`_05` 起的构件化）。"""
    from components.context.tokens import estimate_tokens as shared
    from plugins.context_meter import estimate_tokens as from_plugin

    assert shared is from_plugin
    assert message_tokens(user("你好")) > 0


def test_checkpoint_validates_required_fields(tmp_path: Path):
    from components.session.events import Event, validate

    with pytest.raises(ValueError):
        validate(Event(seq=1, type="compact/checkpoint", data={"summary": "只有摘要"}))
    validate(Event(seq=1, type="compact/checkpoint", data={"summary": "s", "folded": 3}))

    # 折叠越界（folded 超过当时的消息数）在投影时报错——日志被改过就不该假装能读
    from components.session.projection import derive_messages

    events = [
        Event(seq=1, type="user/message", data={"content": "只有一条"}),
        Event(seq=2, type="compact/checkpoint", data={"summary": "s", "folded": 5}),
    ]
    with pytest.raises(ValueError):
        derive_messages(events)


def test_checkpoint_survives_jsonl_round_trip(tmp_path: Path):
    """checkpoint 要能原样落盘再读回（重放同构的物理前提）。"""
    booted = _boot(tmp_path, turns=6)
    for index in range(6):
        booted.agent.ask(f"第{index}问：" + BIG, session_id="s1")
    booted.close()

    path = tmp_path / "s" / "s1" / "session.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    checkpoints = [line for line in lines if line["type"] == "compact/checkpoint"]
    assert checkpoints
    assert "summary" in checkpoints[0]["data"]

    # 投影只看日志也要能折叠（不依赖任何内存状态）
    from components.session.events import Event

    events = [Event.from_dict(line) for line in lines]
    messages = derive_messages(events)
    assert any("【早前对话摘要】" in (m.content or "") for m in messages)
    assert sum(1 for m in messages if m.role == "user") < 6   # 已折叠
    pairs = conversation_pairs(events)
    assert pairs[0][1].role == "user"                          # 摘要在最前（system 之后）


# ---------------------------------------------------------------------------
# 手动触发（`/compact`）：跳过阈值、折到最后一轮之前
# ---------------------------------------------------------------------------


def test_manual_skips_threshold_and_folds_up_to_last_turn(tmp_path: Path):
    """1M 窗口下自动**永不**触发；手动按下就折——折到最后一轮之前。

    这正是两种触发方式的分工：自动管"快撑爆了"，手动管"我就是要现在压"。
    """
    from components.agent.compact import last_turn_start

    booted = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)  # 1M
    opened = booted.agent.open("m1")
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="m1")

    events = opened.session.events
    assert not [e for e in events if e.type == "compact/checkpoint"], "1M 窗口下不该自动触发"

    floor = last_turn_start(opened.messages)
    assert floor > 1, "构造出来的历史应该不止一轮"

    outcome = booted.agent.compact("m1")
    assert outcome is not None
    assert outcome.trigger == "manual"
    assert outcome.folded == floor - 1        # 折掉"最后一轮之前"的全部对话消息

    # 最后一轮原样保留；摘要顶在最前
    roles = [(m.role, m.content) for m in opened.messages]
    assert roles[0][0] == "system"
    assert roles[1][1].startswith("【早前对话摘要】")
    assert any("第2问" in (c or "") for _, c in roles), "最后一轮的提问必须还在"
    booted.close()


def test_manual_keeps_last_turn_and_log_untouched(tmp_path: Path):
    booted = _boot(tmp_path, turns=4, capacity=DEFAULT_CONTEXT_WINDOW)
    opened = booted.agent.open("m2")
    for index in range(4):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="m2")

    outcome = booted.agent.compact("m2")
    assert outcome is not None
    events = opened.session.events
    # 日志原样：4 条 user 一条不少
    assert sum(1 for e in events if e.type == "user/message") == 4
    # 折叠声明进日志，字段齐备
    checkpoint = [e for e in events if e.type == "compact/checkpoint"][-1]
    assert checkpoint.data["folded"] == outcome.folded
    # 投影与内存历史逐条一致
    assert [(m.role, m.content) for m in opened.messages] == \
           [(m.role, m.content) for m in opened.loop.history]
    booted.close()


def test_manual_replay_is_isomorphic(tmp_path: Path):
    """手动压缩同样要过"重放同构"这一关（与自动路径同一套日志语义）。"""
    booted = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)
    opened = booted.agent.open("m3")
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="m3")
    assert booted.agent.compact("m3") is not None
    online = [(m.role, m.content) for m in opened.loop.history]
    booted.close()

    reopened = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)
    resumed = reopened.agent.open("m3")
    assert resumed.resumed is True
    assert [(m.role, m.content) for m in resumed.loop.history] == online
    reopened.close()


def test_manual_noop_when_only_one_turn(tmp_path: Path):
    """只有当前一轮：没什么可折的 → None（入口据此回一句"没有可压缩的历史"）。"""
    booted = _boot(tmp_path, turns=1, capacity=DEFAULT_CONTEXT_WINDOW)
    booted.agent.ask("唯一的一问：" + "内容。" * 80, session_id="m4")
    assert booted.agent.compact("m4") is None
    booted.close()


def test_manual_twice_without_new_turns_is_a_noop(tmp_path: Path):
    """连按两次：第二次只剩上一次的摘要可折 → 空折守卫拦住（不白花一次调用）。

    回归背景：初版实现会把"上一版摘要"再摘一遍——收益约等于零，却多花一次
    模型调用，还可能越摘越薄（实测踩到）。
    """
    booted = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="m5")
    assert booted.agent.compact("m5") is not None
    assert booted.agent.compact("m5") is None, "空折没有被拦住"
    booted.close()


def test_manual_after_new_turn_swallows_previous_summary(tmp_path: Path):
    """再聊一轮后手动压：旧摘要 + 它之后的内容一起折进新摘要（仍只有一条摘要）。"""
    booted = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="m6")
    booted.agent.compact("m6")
    booted.agent.ask("第四问：" + "内容。" * 60, session_id="m6")

    assert booted.agent.compact("m6") is not None
    opened = booted.agent.open("m6")
    summaries = [m for m in opened.messages if (m.content or "").startswith("【早前对话摘要】")]
    assert len(summaries) == 1, f"投影里应只剩一条摘要，实到 {len(summaries)} 条"
    booted.close()


def test_choose_manual_cut_ignores_keep_budget():
    """手动切点不看保留预算（1M 下 10% = 10 万 token，按预算切等于不折）。

    `cut` 是**保留区起点**（折叠区间是 [start, cut)）：cut 越小折得越少。
    测试消息都很小，1M 的保留预算永远攒不满 → 自动切点一路退到 start
    （等于什么都不折）；手动切点则停在"最后一轮之前"，把前面的都折掉。
    """
    from components.agent.compact import choose_manual_cut, last_turn_start

    messages = [system("S"), *[m for i in range(5) for m in (user(f"u{i}"), assistant(f"a{i}"))]]
    policy = CompactionPolicy(capacity=DEFAULT_CONTEXT_WINDOW)   # 1M
    floor = last_turn_start(messages)
    auto_cut = choose_cut(messages, 1, policy, floor=floor)
    manual_cut = choose_manual_cut(messages, 1, policy, floor=floor)
    assert auto_cut == 1, "自动模式在 1M 下应该什么都不折（保留预算永远攒不满）"
    assert manual_cut > auto_cut, "手动模式应该折掉更多（不受保留预算限制）"
    assert manual_cut <= floor, "手动切点也不能越进最后一轮"


def test_manual_cut_never_splits_tool_pairs():
    from components.agent.compact import choose_manual_cut

    messages = [
        system("S"),
        user("查一下"),
        assistant("", [ToolCall(id="c1", name="read", arguments={})]),
        tool_result("c1", {"content": "结果"}),
        user("最后一问"),
        assistant("答"),
    ]
    cut = choose_manual_cut(messages, 1, CompactionPolicy(capacity=DEFAULT_CONTEXT_WINDOW), floor=4)
    assert messages[cut].role != "tool", "切点落在了 tool 消息上——成对保护失效"


def test_manual_raises_when_no_compactor():
    """没有压缩器时手动压缩要报错（用户动作不静默），而不是假装成功。"""
    from components.agent.loop import AgentLoop

    loop = AgentLoop(None, lambda name, args: {}, system_prompt="S")
    with pytest.raises(RuntimeError):
        loop.compact_now([])


def test_manual_failure_surfaces_to_caller(tmp_path: Path):
    """手动路径的摘要失败要往上抛（与自动路径的 fail-soft 相对）。"""
    from components.agent.compact import Compactor

    class _Broken:
        def complete(self, request):
            raise RuntimeError("摘要服务挂了")

    booted = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="m7")
    opened = booted.agent.open("m7")
    opened.loop._compactor = Compactor(_Broken(), CompactionPolicy(capacity=1_048_576))
    with pytest.raises(RuntimeError):
        opened.compact()
    booted.close()


# ---------------------------------------------------------------------------
# 手动压缩的两个入口：webui 路由 + 页面上的按钮/斜杠命令
# ---------------------------------------------------------------------------


def test_webui_compact_route_streams_result(tmp_path: Path):
    """POST /api/sessions/<id>/compact → NDJSON 帧；成功以 __compact_done 收尾。"""
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws")
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url.rstrip("/")
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="w1")

    request = urllib.request.Request(
        url + "/api/sessions/w1/compact", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
    frames = [json.loads(line) for line in body.splitlines() if line.strip()]
    kinds = [frame["kind"] for frame in frames]
    assert "compact_applied" in kinds, f"没看到压缩帧：{kinds}"
    done = [frame for frame in frames if frame["kind"] == "__compact_done"]
    assert done and done[0]["payload"]["folded"] >= 1
    # 页面上有命令菜单容器与 /compact 命令声明（无专属按钮：敲 / 提示）
    with urllib.request.urlopen(url + "/", timeout=8) as response:
        html = response.read().decode("utf-8")
    assert "cmdmenu" in html and "compact-btn" not in html
    with urllib.request.urlopen(url + "/app.js", timeout=8) as response:
        js = response.read().decode("utf-8")
    assert '"/compact"' in js and "COMMANDS" in js and "renderCmdMenu" in js
    booted.close()


def test_command_menu_is_declarative_not_a_dedicated_button(tmp_path: Path):
    """快捷命令走"输入 / 提示"而不是专属按钮：命令是一张声明表，加命令只加一行。

    回归背景：`/compact` 最初做成了输入框左侧的一个专属按钮。用户要求改成
    "敲 / 时主动提示所有命令"——这样 `/goal` 之类的新命令只需要往 COMMANDS
    里加一行，提示、前缀过滤、↑↓ 选择、Tab 补全、Enter 执行全部自动，
    既不用摆第二个按钮，也不会在壳里堆一排各管一摊的控件。
    """
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    with urllib.request.urlopen(url + "/app.js", timeout=8) as response:
        js = response.read().decode("utf-8")
    with urllib.request.urlopen(url, timeout=8) as response:
        html = response.read().decode("utf-8")

    assert "compact-btn" not in html, "专属按钮应已移除"
    assert 'id="cmdmenu"' in html, "命令菜单容器应在页面上"

    # 命令表是声明式的（一条 = 名字 + 说明 + 执行函数）
    assert "const COMMANDS = [" in js
    assert "function cmdCandidates(" in js      # 前缀过滤
    assert "function renderCmdMenu(" in js      # 弹菜单
    assert "function runCommand(" in js         # 执行
    for key in ("ArrowDown", "ArrowUp", "Tab", "Escape"):   # 键盘契约
        assert key in js, f"键盘支持缺少 {key}"
    assert "COMMANDS.find(" in js and "c.name === text" in js   # 命令名直接执行
    assert "takesArgs" in js                    # `_06`：带参数的命令（/todo）
    assert 'name: "/todo"' in js                # 第二条命令已在表里
    booted.close()


def test_webui_compact_noop_reports_plainly(tmp_path: Path):
    """没有可压的历史：以 __compact_noop 明确收尾（不是假装压缩成功）。"""
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws")
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url.rstrip("/")
    booted.agent.ask("唯一的一问", session_id="w2")
    request = urllib.request.Request(
        url + "/api/sessions/w2/compact", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
    kinds = [json.loads(line)["kind"] for line in body.splitlines() if line.strip()]
    assert "__compact_noop" in kinds, f"帧序列：{kinds}"
    booted.close()


def test_webui_compact_failure_reports_error_frame(tmp_path: Path):
    """摘要失败：手动路径要回 __error 帧（人看得见），而不是静默 200。"""
    from components.agent.compact import Compactor

    class _Broken:
        def complete(self, request):
            raise RuntimeError("摘要服务挂了")

    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws")
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url.rstrip("/")
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="w3")

    # 换掉压缩器（新会话用新组合；已打开的会话手动换掉它的引用）
    booted.ctx.retract(COMPACTER_SERVICE)
    broken = Compactor(_Broken(), CompactionPolicy(capacity=1_048_576))
    booted.ctx.provide(COMPACTER_SERVICE, broken)
    booted.agent.open("w3").loop._compactor = broken

    request = urllib.request.Request(
        url + "/api/sessions/w3/compact", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
    frames = [json.loads(line) for line in body.splitlines() if line.strip()]
    errors = [frame for frame in frames if frame["kind"] == "__error"]
    assert errors, f"帧序列：{[f['kind'] for f in frames]}"
    assert "压缩失败" in errors[0]["payload"]["message"]
    booted.close()


def test_compact_kernel_event_carries_trigger(tmp_path: Path):
    """`context/compacted` 事件要带上 trigger，界面才分得清手动/自动。"""
    seen = []
    booted = _boot(tmp_path, turns=3, capacity=DEFAULT_CONTEXT_WINDOW)
    booted.ctx.on("context/compacted", lambda **payload: seen.append(payload))
    for index in range(3):
        booted.agent.ask(f"第{index}问：" + "内容。" * 60, session_id="k1")
    booted.agent.compact("k1")
    assert [item["trigger"] for item in seen if item["status"] == "applied"] == ["manual"]
    booted.close()


def test_manual_summary_is_plain_text_not_tool_messages(tmp_path: Path):
    """手动压缩的摘要请求同样摊平成文本（与自动路径共用一套构件）。"""
    from components.agent.compact import build_summary_request

    request = build_summary_request([user("第一句"), assistant("答复")], SMALL)
    assert request.tools == []
    assert [m.role for m in request.messages] == ["system", "user"]


__all__ = ["SMALL"]
