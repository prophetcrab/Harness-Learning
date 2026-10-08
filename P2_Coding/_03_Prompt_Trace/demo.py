"""_03_Prompt_Trace 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + M1–M3 的组装验收（基线行为回归）：

    0a. [M4 累积] 变量插值：{{cwd}}/{{platform}}/{{time}}；改 cwd 只影响对应 section
    0b. [M4 _03 新增] 装配单与重建断言：
        capture_trace 记下"配料表"→ 序列化（模拟进日志）→ rebuild_text 原样重建；
        检验有牙齿：篡改记录对不上、抹掉变量 fail loud
    0c. 每 step 渲染 + 装配单进日志：每一步"模型收到的文本 == 由该步日志重建的文本"
    1. 新会话，多轮对话（主故事用固定上下文，保证可复现；装配单在 session/start）
    2. 打印会话日志；3. 模拟退出；4. resume 接续；5. 同构断言；6. 分叉
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat import latest_trace_with_source, show_trace

from context import (
    RuntimeContext,
    collect_runtime_context,
    effective_system_prompt,
    latest_prompt_trace,
    open_context_harness,
    project,
)
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.runner import fork_session, load_messages
from harness.session import JsonlStore
from harness.tools import ScriptedApprover
from harness.tools.approval import ApprovalDecision
from prompt import (
    PromptAssembler,
    Section,
    SectionRegistry,
    capture_trace,
    default_sections,
    rebuild_text,
)
from prompt.interpolate import render_text

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志\n- M4 提示词装配单"


class StepClock:
    """每采样一次推进 30 秒——演示"每 step 渲染时时间真的在走"。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=30)
        return self._now


def story_context() -> RuntimeContext:
    """主故事用的固定上下文（确定性；cwd 指向工作区，平台给定 DemoOS）。"""
    return collect_runtime_context(
        WORKSPACE, now=lambda: STORY_MOMENT, platform_name="DemoOS"
    )


def plain(messages):
    """把消息压成可比较的元组序列（同构断言用）。"""
    return [
        (m.role, m.content, tuple((c.id, c.name) for c in m.tool_calls), m.tool_call_id)
        for m in messages
    ]


def main() -> None:
    keep = "--keep" in sys.argv
    if DEMO_RUN.exists() and not keep:
        shutil.rmtree(DEMO_RUN)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # =====================================================================
    # 0a) [M4 累积] 变量插值：{{cwd}} / {{platform}} / {{time}}
    # =====================================================================
    print("=" * 68)
    print("0a) 变量插值（_02 机制，沿用；全离线）")
    registry = SectionRegistry()
    for section in default_sections():
        registry.register(section)
    assembler = PromptAssembler(registry)
    print(f"   默认 section：{[section.name for section in assembler.parts()]}")

    vars_a = {"cwd": "D:/ws-a", "platform": "DemoOS", "time": "2026-10-08T09:00:00+08:00"}
    vars_b = {**vars_a, "cwd": "D:/ws-b"}
    changed = [
        section.name
        for section in assembler.parts()
        if render_text(section.content, vars_a) != render_text(section.content, vars_b)
    ]
    print(f"   改 cwd（D:/ws-a → D:/ws-b）后变化的节：{changed}（只影响引用它的 section）")

    # =====================================================================
    # 0b) [本阶段新增] 装配单与重建断言
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) 装配单（trace）与重建 —— 本阶段新增机制")
    registry2 = SectionRegistry()
    for section in default_sections():
        registry2.register(section)
    scope = registry2.scoped("demo-session")
    scope.register(Section("conversation", "演示作用域追加的一节。"))
    assembler2 = PromptAssembler(scope)
    variables = {"cwd": "D:/ws", "platform": "DemoOS", "time": "2026-10-08T09:00:00+08:00"}

    trace = capture_trace(assembler2, variables)
    print(f"   装配单记录了 {len(trace.sections)} 节；每节的来源：")
    for entry in trace.sections:
        print(f"     - {entry.name}  来源={entry.source}  引用变量={list(entry.referenced) or '无'}")

    # 序列化再读回：模拟"随事件进了日志、事后从磁盘读出来"
    data = json.loads(json.dumps(trace.to_dict()))
    rebuilt = rebuild_text(data)
    assert rebuilt == data["text"]
    print("   ✓ 重建一致：rebuild_text(日志里的装配单) == 记录文本")
    print("     （重建由『记录数据』驱动：新建注册表、注册模板、再用装配器拼接）")

    # 检验有牙齿 1：改掉记录文本 → 重建对不上
    tampered = {**data, "text": data["text"] + "（被篡改）"}
    print(f"   篡改记录文本后：重建 == 记录 ? {rebuild_text(tampered) == tampered['text']}")
    # 检验有牙齿 2：抹掉一个变量 → fail loud
    broken = json.loads(json.dumps(data))
    broken["variables"].pop("time")
    try:
        rebuild_text(broken)
    except ValueError as exc:
        print(f"   抹掉变量 time 后 fail loud：{exc}")

    # =====================================================================
    # 0c) 每 step 渲染 + 装配单进日志（脚本时钟：每采样一次走 30 秒）
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 每 step 渲染 + 装配单进日志（脚本时钟驱动）")
    ctx_ws = DEMO_RUN / "ctx_workspace"
    ctx_ws.mkdir(parents=True, exist_ok=True)
    clock = StepClock(STORY_MOMENT)
    inner = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "6*7"}),
            text_reply("6 × 7 = 42。"),
        ]
    )
    ctx = open_context_harness(
        "ctx",
        provider=inner,
        root=DEMO_RUN / "ctx_sessions",
        workspace=ctx_ws,
        context_source=lambda: collect_runtime_context(ctx_ws, now=clock, platform_name="DemoOS"),
    )
    ctx.harness.send("帮我算 6*7")
    inner.assert_all_consumed()

    print("   事件序列（<-trace 表示该事件携带装配单）：")
    for event in ctx.session.events:
        mark = "  <-trace" if event.data.get("prompt_trace") else ""
        print(f"     #{event.seq:>2} {event.type}{mark}")

    events = ctx.session.events
    seen = [request.messages[0].content for request in inner.requests]
    rebuilt_steps = [
        rebuild_text(latest_prompt_trace(events[: index + 1]))
        for index, event in enumerate(events)
        if event.type == "assistant/message"
    ]
    assert rebuilt_steps == seen, "由日志重建的文本与模型实际收到的文本不一致！"
    print("   ✓ 每一步：模型收到的文本 == rebuild(该步之前最近的装配单)")
    assert rebuild_text(latest_prompt_trace(events)) == effective_system_prompt(events)
    print("   ✓ 当前生效文本 == rebuild(当前生效装配单)")
    trace_data, label = latest_trace_with_source(events)
    show_trace(trace_data, "当前生效的装配单", f"来自日志 {label}")

    # =====================================================================
    # 1) 新会话：多轮对话（工具 + 审批 + 闲聊），事件逐条落盘
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 新会话，多轮对话（FakeLLM 剧本驱动；固定上下文，保证可复现）")
    provider = FakeLLM(
        [
            # turn 1：算数（走 calculate 工具）
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52。"),
            # turn 2：写文件（需审批，本 demo 用剧本审批直接批准）
            tool_call_reply("write_file", {"path": "notes/todo.txt", "content": NOTE}),
            text_reply("已把笔记写入 notes/todo.txt。"),
            # turn 3：闲聊（无工具）
            text_reply("好的，我记住了。"),
        ]
    )
    approval = ScriptedApprover([ApprovalDecision(True, "demo：批准写入")])
    ctx = open_context_harness(
        "demo",
        provider=provider,
        root=SESSIONS,
        workspace=WORKSPACE,
        approval=approval,
        context_source=story_context,
    )
    for question in ["帮我算 1234*56.78", "把要点记到 notes/todo.txt", "记住了吗？"]:
        result = ctx.harness.send(question)
        print(f"   你：{question}")
        print(f"   助手：{result.final_text}")

    online_history = plain(ctx.harness.history)  # 供第 5 步同构对照
    provider.assert_all_consumed()
    approval.assert_all_consumed()
    print(f"   在线历史角色序列：{[m.role for m in ctx.harness.history]}")

    # =====================================================================
    # 2) 打印会话日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("2) 会话日志（append-only 事件序列，逐条落盘）")
    for event in ctx.session.events:
        mark = "  <-trace" if event.data.get("prompt_trace") else ""
        print(f"   #{event.seq:>2} {event.type}{mark}")
    updates = [e for e in ctx.session.events if e.type == "system/message"]
    print(f"   system/message 事件：{len(updates)} 条（固定上下文 → 渲染无变化 → 不重复记录）")
    print("   装配单在 session/start 里（开场那份）——可用 /prompt 或 --dump-prompt 查看")
    print(f"   日志文件：{JsonlStore(SESSIONS).path('demo')}")

    # =====================================================================
    # 3) 模拟"退出"：丢掉进程内对象，只剩磁盘上的日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("3) 退出：丢弃进程内的 harness 对象（历史只留在磁盘日志里）")
    del ctx
    print("   进程内已无任何会话状态。")

    # =====================================================================
    # 4) resume：同一个 session_id 重新打开
    # =====================================================================
    print("\n" + "=" * 68)
    print("4) resume：重新打开同一个会话（对旧 id 是恢复，对新 id 是创建 —— 同一段代码）")
    provider2 = FakeLLM(
        [
            # turn 4：列出工作区文件（证明第 1 步写的文件还在）
            tool_call_reply("list_files", {"path": "."}),
            text_reply("工作区里有 notes/todo.txt。"),
            # turn 5：读回文件内容
            tool_call_reply("read_file", {"path": "notes/todo.txt"}),
            text_reply("已读回笔记内容，确认落盘成功。"),
        ]
    )
    resumed_ctx = open_context_harness(
        "demo",
        provider=provider2,
        root=SESSIONS,
        workspace=WORKSPACE,
        approval=ScriptedApprover([]),
        context_source=story_context,
    )
    resumed = resumed_ctx.harness
    print(f"   resume 时的历史角色序列：{[m.role for m in resumed.messages]}")
    print(f"   接续 turn 编号：{resumed.session.last_turn_number}")
    r4 = resumed.send("工作区里有哪些文件？")
    print(f"   你：工作区里有哪些文件？\n   助手：{r4.final_text}")
    r5 = resumed.send("读一下 notes/todo.txt")
    print(f"   你：读一下 notes/todo.txt\n   助手：{r5.final_text}")
    print(f"   接续后 turn 编号：{r5.turn}（从 resume 前接续，不是从 1 重来）")
    provider2.assert_all_consumed()

    # =====================================================================
    # 5) 同构断言 + 本阶段的重建断言
    # =====================================================================
    print("\n" + "=" * 68)
    print("5) 同构断言：重放日志得到的历史 == 在线产生的历史")
    replayed = plain(load_messages("demo", JsonlStore(SESSIONS)))
    assert replayed == plain(resumed.history), "重放历史与在线历史不一致！"
    # 第 1 步（退出前）的在线快照必须是重放历史的前缀 —— 证明历史是"长出"的，不是被改写的
    assert replayed[: len(online_history)] == online_history, "前期在线历史与重放前缀不一致！"
    # 本阶段的扩展投影（考虑 system/message 遮蔽）同样与在线历史一致
    extended = plain(project(resumed_ctx.session.events))
    assert extended == plain(resumed.history), "扩展投影与在线历史不一致！"
    print("   ✓ 一致（消息历史完全由日志投影而来，且前期快照是重放前缀）")
    print("   ✓ 扩展投影（最近一次 system/message 生效）与在线历史一致")
    # ★ 本阶段断言：当前生效的提示词可由日志里的装配单重建
    latest = latest_prompt_trace(resumed_ctx.session.events)
    assert rebuild_text(latest) == effective_system_prompt(resumed_ctx.session.events)
    assert rebuild_text(latest) == replayed[0][1]  # 重放历史的第一条（system 消息）内容
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（本阶段新增的「可由日志重建」）")

    # =====================================================================
    # 6) 分叉
    # =====================================================================
    print("\n" + "=" * 68)
    print("6) 分叉：从第 6 条事件处派生一个新会话")
    store = JsonlStore(SESSIONS)
    forked = fork_session(store, "demo", "demo-fork", upto_seq=6)
    print(f"   demo-fork 事件：{[e.type for e in forked.events]}")
    print(f"   原会话 demo 仍是 {len(resumed.session.events)} 条（未被改动）")
    print(f"   现有会话：{store.list_sessions()}")

    print("\n全部步骤完成。可再试：python chat.py --fake --ask 「现在几点？」 --dump-prompt")


if __name__ == "__main__":
    main()
