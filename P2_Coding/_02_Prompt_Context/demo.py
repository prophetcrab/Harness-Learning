"""_02_Prompt_Context 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + M1–M3 的组装验收（基线行为回归）：

    0. [M4 _02 新增] 变量插值与"每 step 渲染"
       0a 机械演示：{{cwd}}/{{platform}}/{{time}} 渲染；改 cwd 只影响引用它的 section；
          未知变量 fail loud（全部离线，不需要模型）
       0b 集成小剧场：脚本时钟推进 → 每步渲染都不同 → system/message 进日志；
          断言"模型每步实际看到的文本 == 由日志重建的文本"；投影里最近一次渲染生效
    1. 新会话，多轮对话：算数（工具）、写文件（工具 + 审批）、闲聊
       （主故事用**固定上下文**——渲染无变化 → 日志里没有多余 system/message，可复现）
    2. 打印会话日志：能看到 session/start、turn/step、assistant/message、tool/result
    3. 模拟"退出"：丢掉进程内对象，只留下磁盘上的日志
    4. resume：同一个 session_id 重新打开 —— 历史完整、turn 编号接续
    5. 恢复后继续用工具（list_files / read_file），验证第 1 步写入的文件还在
    6. 同构断言：重放日志得到的历史 == 在线产生的历史
    7. 分叉：从某个 seq 派生新会话，原会话不受影响
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context import (
    RuntimeContext,
    collect_runtime_context,
    effective_system_prompt,
    open_context_harness,
    project,
)
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.runner import fork_session, load_messages
from harness.session import JsonlStore
from harness.tools import ScriptedApprover
from harness.tools.approval import ApprovalDecision
from prompt import PromptAssembler, SectionRegistry, default_sections, render_text

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志\n- M4 提示词每 step 渲染"


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
    # 0a) [M4 _02 新增] 变量插值：{{cwd}} / {{platform}} / {{time}}
    # =====================================================================
    print("=" * 68)
    print("0a) 变量插值（本阶段新增的最小机制，全离线）")
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
    print(f"   改 cwd（D:/ws-a → D:/ws-b）后变化的节：{changed}")
    print("   → 验收：改 cwd 只影响引用它的 section，其它节逐字节不变。")
    env_line = next(
        line for line in assembler.assemble(vars_b).splitlines() if "运行时环境" in line
    )
    print(f"   渲染一节示例（env）：{env_line}")
    try:
        render_text("引用了 {{missing}}", {})
    except ValueError as exc:
        print(f"   fail loud 演示：{exc}")

    # =====================================================================
    # 0b) 每 step 渲染：上下文变化 → system/message 进日志（脚本时钟驱动）
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) 每 step 渲染 + system/message 进日志（脚本时钟：每采样一次走 30 秒）")
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
        context_source=lambda: collect_runtime_context(
            ctx_ws, now=clock, platform_name="DemoOS"
        ),
    )
    ctx.harness.send("帮我算 6*7")
    inner.assert_all_consumed()

    print("   事件序列（每一步：step/start → system/message → assistant/message）：")
    for event in ctx.session.events:
        print(f"     #{event.seq:>2} {event.type}")

    events = ctx.session.events
    seen = [request.messages[0].content for request in inner.requests]
    rebuilt = [
        effective_system_prompt(events[: index + 1])
        for index, event in enumerate(events)
        if event.type == "assistant/message"
    ]
    assert "09:01:00" in seen[0] and "09:01:30" in seen[1]  # 两步的渲染时刻不同
    assert rebuilt == seen, "由日志重建的文本与模型实际收到的文本不一致！"
    print("   ✓ 模型两步实际看到的文本 == 由日志（最近一次 system/message）重建的文本")
    latest_line = next(
        line
        for line in project(events)[0].content.splitlines()
        if "当前时间" in line
    )
    print(f"   ✓ 投影视图共 {len(project(events))} 条消息，system 是最近一次渲染：")
    print(f"     {latest_line}")

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

    online_history = plain(ctx.harness.history)  # 供第 6 步同构对照
    provider.assert_all_consumed()
    approval.assert_all_consumed()
    print(f"   在线历史角色序列：{[m.role for m in ctx.harness.history]}")

    # =====================================================================
    # 2) 打印会话日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("2) 会话日志（append-only 事件序列，逐条落盘）")
    for event in ctx.session.events:
        print(f"   #{event.seq:>2} {event.type}")
    updates = [e for e in ctx.session.events if e.type == "system/message"]
    print(f"   system/message 事件：{len(updates)} 条（固定上下文 → 渲染无变化 → 不重复记录）")
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
    # 5) 同构断言
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

    print("\n全部步骤完成。可再试：python chat.py --fake / python -m harness list / show demo")


if __name__ == "__main__":
    main()
