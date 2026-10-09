"""_07_Plugin_Effect 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + 前序机制回归（改动任何底层后都该保持全绿）：

    0a. [M6 _07 新增] 注册即 effect：装载 → 登记 → 卸载自动回卷（同级 LIFO）
    0b. effect 成树：嵌套 effect 子先父后回卷；批量装载遇故障不留半装状态
    0c. 装配对照：ServiceContainer（手工登记，没有撤销通道） vs 插件装载
        （ctx 台账，可整体/单个卸载——"注册即 effect"的落点）
    1. 新会话（插件装载：fs + subprocess + toolbox），多轮对话：算数 /
       真实执行命令（审批）/ 写文件（审批）/ 闲聊；结束后整体卸载看回卷
    2. 打印会话日志；3. 模拟退出；4. resume 接续；5. 同构断言；6. 分叉
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
    latest_prompt_trace,
    open_context_harness,
    project,
)
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.runner import fork_session, load_messages
from harness.session import JsonlStore
from harness.tools import ScriptedApprover
from harness.tools.approval import ApprovalDecision
from kernel import Context, load_plugins
from prompt import rebuild_text
from providers import (
    FS_NOT_FOUND,
    TOOLBOX_CAPABILITY,
    CommandResult,
    LocalFS,
    LocalSubprocess,
    MemoryFS,
    ScriptedSubprocess,
    boot_toolbox,
    build_toolbox,
    make_fs_plugin,
    make_subprocess_plugin,
    make_toolbox_plugin,
)

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志\n- M4 提示词机制\n- M5 能力接缝\n- M6 插件 effect"


def story_context() -> RuntimeContext:
    """主故事用的固定上下文（确定性；cwd 指向工作区，平台给定 DemoOS）。"""
    return collect_runtime_context(WORKSPACE, now=lambda: STORY_MOMENT, platform_name="DemoOS")


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
    # 0a) [本阶段新增] 注册即 effect：装载 → 登记 → 卸载自动回卷
    # =====================================================================
    print("=" * 68)
    print("0a) Context / effect：注册动作自带回卷（铁律 #3）")
    trace: list[str] = []
    demo_ctx = Context()

    def greeting_plugin(context: Context):
        context.provide("greeting", "你好")
        trace.append("装 greeting（登记槽位）")

        def dispose() -> None:
            context.retract("greeting")
            trace.append("撤 greeting（回卷槽位）")

        return dispose

    dispose_greeting = demo_ctx.effect("greeting-plugin", lambda: greeting_plugin(demo_ctx))
    print(f"   装载后：槽位={demo_ctx.slots}  effect={demo_ctx.effect_names}")
    print(f"           greet() = {demo_ctx.require('greeting')!r}")
    dispose_greeting()
    print(f"   卸载后：槽位={demo_ctx.slots}  effect={demo_ctx.effect_names}")
    print(f"   回卷记录：{trace}")

    # 同级 LIFO：装了 A、B 两样东西，撤销按后进先出
    trace2: list[str] = []
    lifo_ctx = Context()
    lifo_ctx.effect(
        "plugin-a",
        lambda: (trace2.append("装 A"), lambda: trace2.append("撤 A"))[1],
    )
    lifo_ctx.effect(
        "plugin-b",
        lambda: (trace2.append("装 B"), lambda: trace2.append("撤 B"))[1],
    )
    lifo_ctx.unload("plugin-a")
    lifo_ctx.unload("plugin-b")
    print(f"   多个 effect 的撤销顺序：{trace2}（同级后进先出）")

    # =====================================================================
    # 0b) effect 成树：嵌套回卷 + 故障不留半装状态
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) effect 树与故障：子先父后；装载失败自动清干净")
    trace3: list[str] = []
    tree_ctx = Context()

    def outer_plugin(context: Context):
        trace3.append("装 outer")
        # 插件内部再注册一个小 effect（装载期注册 → 自动成为子 effect）
        context.register(
            lambda: (
                trace3.append("装 inner"),
                lambda: trace3.append("撤 inner"),
            )[1]
        )

        def dispose() -> None:
            trace3.append("撤 outer")

        return dispose

    tree_ctx.effect("outer", lambda: outer_plugin(tree_ctx))
    tree_ctx.unload("outer")
    print(f"   嵌套回卷顺序：{trace3}")
    print("   → 子 effect 先撤、父 effect 后撤（Cordis：拆卸按预期顺序展开）")

    fault_ctx = Context()

    def good_plugin(context: Context):
        context.provide("ok", True)
        return lambda: context.retract("ok")

    def broken_plugin(context: Context):
        context.provide("half", "半装")
        raise RuntimeError("这个插件在装载时炸了")

    try:
        load_plugins(fault_ctx, [good_plugin, broken_plugin])
    except RuntimeError as exc:
        print(f"   批量装载遇故障：{exc}")
    print(f"   装载失败后槽位：{fault_ctx.slots}（已装部分被回卷，没有半装状态）")

    # =====================================================================
    # 0c) 装配对照：手工登记（_06） vs 插件装载（本阶段）
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 装配对照：ServiceContainer（手工登记） vs 插件装载（注册即 effect）")
    scripted = ScriptedSubprocess([CommandResult(0, "ok\n")])

    # 旧形态：直接构造（构造完就完了——没有东西可撤销）
    manual = build_toolbox(MemoryFS(), scripted, workspace="/demo/ws")
    print(f"   手工登记：工具面={manual.names}（没有撤销通道）")

    # 新形态：插件装载（整条装配进台账，可整体卸载）
    scripted = ScriptedSubprocess([CommandResult(0, "ok\n")])
    plug_ctx, toolbox = boot_toolbox(MemoryFS(), scripted, workspace="/demo/ws")
    print(f"   插件装载：工具面={toolbox.names}")
    print(f"             槽位={plug_ctx.slots}")
    print(f"             effect 台账={plug_ctx.effect_names}")
    plug_ctx.unload("plugin:toolbox")
    print(f"   卸载 toolbox 插件 → 槽位={plug_ctx.slots}（工具面被回卷，其余不动）")
    load_plugins(plug_ctx, [make_toolbox_plugin(workspace="/demo/ws")])
    print(f"   重新装载 → 槽位={plug_ctx.slots}（装配可反复装卸）")
    for name in reversed(plug_ctx.effect_names):
        plug_ctx.unload(name)
    print(f"   整体卸载 → 槽位={plug_ctx.slots}  effect={plug_ctx.effect_names}")

    # =====================================================================
    # 1) 新会话：插件装载（fs + subprocess + toolbox），多轮对话
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 新会话，多轮对话（FakeLLM 剧本；插件装载：LocalFS + LocalSubprocess）")
    story_ctx = Context()
    load_plugins(
        story_ctx,
        [
            make_fs_plugin(LocalFS(WORKSPACE)),
            make_subprocess_plugin(LocalSubprocess()),
            make_toolbox_plugin(workspace=WORKSPACE, shell_timeout=15),
        ],
    )
    story_registry = story_ctx.require(TOOLBOX_CAPABILITY)
    print(f"   插件台账：{story_ctx.effect_names}")
    print(f"   槽位：{story_ctx.slots}")
    print(f"   工具面：{story_registry.names}（从 ctx 取，不再手工构造）")

    command = f'{sys.executable} -c "print(1+1)"'  # 跨平台：不依赖 dir/ls
    provider = FakeLLM(
        [
            # turn 1：算数（calculate 工具）
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52。"),
            # turn 2：真实执行一条命令（需审批，批准）
            tool_call_reply("shell", {"command": command}),
            text_reply("命令执行成功，输出是 2。"),
            # turn 3：写文件（需审批，批准）
            tool_call_reply("write_file", {"path": "notes/todo.txt", "content": NOTE}),
            text_reply("已把笔记写入 notes/todo.txt。"),
            # turn 4：闲聊（无工具）
            text_reply("好的，我记住了。"),
        ]
    )
    approval = ScriptedApprover(
        [
            ApprovalDecision(True, "demo：批准执行命令"),
            ApprovalDecision(True, "demo：批准写入"),
        ]
    )
    ctx = open_context_harness(
        "demo",
        provider=provider,
        root=SESSIONS,
        workspace=WORKSPACE,
        approval=approval,
        context_source=story_context,
        tool_registry=story_registry,
    )
    for question in [
        "帮我算 1234*56.78",
        "用 shell 跑一下数字验证",
        "把要点记到 notes/todo.txt",
        "记住了吗？",
    ]:
        result = ctx.harness.send(question)
        print(f"   你：{question}")
        print(f"   助手：{result.final_text}")

    online_history = plain(ctx.harness.history)  # 供第 5 步同构对照
    provider.assert_all_consumed()
    approval.assert_all_consumed()
    # 命令的输出确实进了模型历史（stdout 里有 2）
    assert any(m.role == "tool" and "2" in m.content for m in ctx.harness.messages)
    print("   ✓ 两次审批（命令 + 写入）都用在了正确的位置；命令输出进了模型历史")

    # ★ 本阶段机制：会话结束后整体卸载——注册物按逆序自动回卷
    print(f"   会话完成，整体卸载插件前：槽位={story_ctx.slots}")
    for name in reversed(story_ctx.effect_names):
        story_ctx.unload(name)
    print(f"   整体卸载后：槽位={story_ctx.slots}  effect={story_ctx.effect_names}")
    print("   → 工具面/能力/台账全部撤销（'注册即 effect'：装得进、撤得干净）")

    # =====================================================================
    # 2) 打印会话日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("2) 会话日志（append-only 事件序列，逐条落盘）")
    for event in ctx.session.events:
        mark = "  <-trace" if event.data.get("prompt_trace") else ""
        print(f"   #{event.seq:>2} {event.type}{mark}")
    print(f"   日志文件：{JsonlStore(SESSIONS).path('demo')}")

    # =====================================================================
    # 3) 模拟"退出"
    # =====================================================================
    print("\n" + "=" * 68)
    print("3) 退出：丢弃进程内的 harness 对象（历史只留在磁盘日志里）")
    del ctx
    print("   进程内已无任何会话状态。")

    # =====================================================================
    # 4) resume（同样走插件装载）
    # =====================================================================
    print("\n" + "=" * 68)
    print("4) resume：重新打开同一个会话（对旧 id 是恢复，对新 id 是创建 —— 同一段代码）")
    provider2 = FakeLLM(
        [
            tool_call_reply("list_files", {"path": "."}),
            text_reply("工作区里有 notes/todo.txt。"),
            tool_call_reply("read_file", {"path": "notes/todo.txt"}),
            text_reply("已读回笔记内容，确认落盘成功。"),
        ]
    )
    resume_ctx = Context()
    load_plugins(
        resume_ctx,
        [
            make_fs_plugin(LocalFS(WORKSPACE)),
            make_subprocess_plugin(LocalSubprocess()),
            make_toolbox_plugin(workspace=WORKSPACE, shell_timeout=15),
        ],
    )
    resumed_ctx = open_context_harness(
        "demo",
        provider=provider2,
        root=SESSIONS,
        workspace=WORKSPACE,
        approval=ScriptedApprover([]),
        context_source=story_context,
        tool_registry=resume_ctx.require(TOOLBOX_CAPABILITY),
    )
    resumed = resumed_ctx.harness
    print(f"   resume 时的历史角色序列：{[m.role for m in resumed.messages]}")
    print(f"   接续 turn 编号：{resumed.session.last_turn_number}")
    r5 = resumed.send("工作区里有哪些文件？")
    print(f"   你：工作区里有哪些文件？\n   助手：{r5.final_text}")
    r6 = resumed.send("读一下 notes/todo.txt")
    print(f"   你：读一下 notes/todo.txt\n   助手：{r6.final_text}")
    print(f"   接续后 turn 编号：{r6.turn}（从 resume 前接续，不是从 1 重来）")
    provider2.assert_all_consumed()

    # =====================================================================
    # 5) 同构断言
    # =====================================================================
    print("\n" + "=" * 68)
    print("5) 同构断言：重放日志得到的历史 == 在线产生的历史")
    replayed = plain(load_messages("demo", JsonlStore(SESSIONS)))
    assert replayed == plain(resumed.history), "重放历史与在线历史不一致！"
    assert replayed[: len(online_history)] == online_history, "前期在线历史与重放前缀不一致！"
    extended = plain(project(resumed_ctx.session.events))
    assert extended == plain(resumed.history), "扩展投影与在线历史不一致！"
    print("   ✓ 一致（消息历史完全由日志投影而来，且前期快照是重放前缀）")
    print("   ✓ 扩展投影（最近一次 system/message 生效）与在线历史一致")
    latest = latest_prompt_trace(resumed_ctx.session.events)
    assert rebuild_text(latest) == effective_system_prompt(resumed_ctx.session.events)
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（提示词机制在插件装配下照常工作）")

    # =====================================================================
    # 6) 分叉 + 结构化错误示例
    # =====================================================================
    print("\n" + "=" * 68)
    print("6) 分叉：从第 6 条事件处派生一个新会话")
    store = JsonlStore(SESSIONS)
    forked = fork_session(store, "demo", "demo-fork", upto_seq=6)
    print(f"   demo-fork 事件：{[e.type for e in forked.events]}")
    print(f"   原会话 demo 仍是 {len(resumed.session.events)} 条（未被改动）")
    print(f"   现有会话：{store.list_sessions()}")

    missing = story_registry.resolve("read_file").func(path="notes/none.txt")
    assert missing["code"] == FS_NOT_FOUND
    print("\n结构化错误示例（接缝错误与文件错误同构——同样的 error + code 两键）：")
    print(f"   {missing}")

    print("\n全部步骤完成。可再试：python chat.py --fake --ask 「用 shell 看看目录」")


if __name__ == "__main__":
    main()
