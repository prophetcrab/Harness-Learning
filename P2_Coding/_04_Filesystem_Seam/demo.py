"""_04_Filesystem_Seam 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + M1–M3 的组装验收（基线行为回归）：

    0a. [M5 _04 新增] 接缝对照：同一组操作在 LocalFS / MemoryFS 上行为一致
    0b. 单槽服务：重复注册报错、未注册 resolve 报错、显式 resolve（铁律 #5/#6）
    0c. 同一段对话、两个 provider：工具结果逐字段相同；磁盘痕迹只有 local 留下
    1. 新会话，多轮对话（工具面已换成接缝工具；LocalFS provider）
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
from prompt import rebuild_text
from providers import (
    FS_CAPABILITY,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    LocalFS,
    MemoryFS,
    ServiceContainer,
    build_filesystem_registry,
)

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志\n- M4 提示词机制\n- M5 能力接缝"


def story_context() -> RuntimeContext:
    """主故事用的固定上下文（确定性；cwd 指向工作区，平台给定 DemoOS）。"""
    return collect_runtime_context(WORKSPACE, now=lambda: STORY_MOMENT, platform_name="DemoOS")


def plain(messages):
    """把消息压成可比较的元组序列（同构断言用）。"""
    return [
        (m.role, m.content, tuple((c.id, c.name) for c in m.tool_calls), m.tool_call_id)
        for m in messages
    ]


def tool_results(result):
    """把一个 turn 的工具结果压成可比较的元组序列。"""
    return [
        (step.index, tuple((r.name, r.result, r.error) for r in step.tool_results))
        for step in result.steps
    ]


def main() -> None:
    keep = "--keep" in sys.argv
    if DEMO_RUN.exists() and not keep:
        shutil.rmtree(DEMO_RUN)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # =====================================================================
    # 0a) [本阶段新增] 接缝对照：同一组操作，两个 provider
    # =====================================================================
    print("=" * 68)
    print("0a) FileSystem 接缝：同一组操作在 LocalFS / MemoryFS 上行为一致")
    fs_local = LocalFS(DEMO_RUN / "fs_demo_ws")
    fs_memory = MemoryFS()
    for name, fs in (("LocalFS ", fs_local), ("MemoryFS", fs_memory)):
        fs.write_text("notes/a.txt", "第一版")
        fs.write_text("notes/b.txt", "另一个文件")
        fs.edit_text("notes/a.txt", "第一版", "第二版")
        content = fs.read_text("notes/a.txt")
        files = fs.list_files(".")
        print(f"   {name}：read={content!r}  list={files}")
        try:
            fs.read_text("missing.txt")
        except Exception as exc:  # noqa: BLE001 —— demo 里展示统一错误
            print(f"            缺文件 → {exc.code}：{exc}")
    print("   → 行为一致；差异只在「数据到哪去」：一个写磁盘，一个写内存。")

    # =====================================================================
    # 0b) 单槽服务：注册一次、显式 resolve（铁律 #5 / #6）
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) ServiceContainer：单槽注册 + 显式 resolve")
    services = ServiceContainer()
    services.register(FS_CAPABILITY, MemoryFS())
    try:
        services.register(FS_CAPABILITY, LocalFS(DEMO_RUN / "other"))
    except ValueError as exc:
        print(f"   重复注册（fail loud）：{exc}")
    fs = services.resolve(FS_CAPABILITY)
    print(f"   显式 resolve：{type(fs).__name__}（『用哪个实现』在解析点决定，不藏在工具里）")
    try:
        services.resolve("llm")
    except KeyError as exc:
        print(f"   未注册就 resolve（fail loud）：{exc}")

    # =====================================================================
    # 0c) 同一段对话、两个 provider：结果相同，副作用不同
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 同一段对话跑两遍（local / memory）：工具结果逐字段相同")

    def script() -> FakeLLM:
        return FakeLLM(
            [
                tool_call_reply("write_file", {"path": "notes/demo.txt", "content": "seam"}),
                text_reply("已写入。"),
                tool_call_reply("read_file", {"path": "notes/demo.txt"}),
                text_reply("读回：seam。"),
                tool_call_reply("list_files", {"path": "."}),
                text_reply("看到了文件。"),
            ]
        )

    def run_once(session_id: str, fs, workspace: Path):
        inner = script()
        ctx = open_context_harness(
            session_id,
            provider=inner,
            root=DEMO_RUN / "seam_sessions",
            workspace=workspace,
            approval=ScriptedApprover([ApprovalDecision(True, "demo：批准写入")]),
            context_source=lambda: collect_runtime_context(
                workspace, now=lambda: STORY_MOMENT, platform_name="DemoOS"
            ),
            tool_registry=build_filesystem_registry(fs),
        )
        results = [
            ctx.harness.send("写个文件"),
            ctx.harness.send("读回来"),
            ctx.harness.send("列一下"),
        ]
        inner.assert_all_consumed()
        return results

    local_ws = DEMO_RUN / "fs_local_ws"
    memory_ws = DEMO_RUN / "fs_memory_ws"  # 仅作"如果真写盘会落哪"的哨兵路径
    local_results = run_once("seam-local", LocalFS(local_ws), local_ws)
    memory_results = run_once("seam-memory", MemoryFS(), memory_ws)

    same = [tool_results(r) for r in local_results] == [tool_results(r) for r in memory_results]
    assert same, "两个 provider 下工具结果不一致！"
    print("   ✓ 逐字段相同：write/read/list 的工具结果两边完全一致")
    print(f"     例：read_file 结果 = {memory_results[1].steps[0].tool_results[0].result}")
    assert (local_ws / "notes/demo.txt").read_text(encoding="utf-8") == "seam"
    print(f"   ✓ LocalFS：文件真的落盘（{local_ws / 'notes' / 'demo.txt'}）")
    assert not memory_ws.exists()
    print("   ✓ MemoryFS：同一个操作留在内存里——磁盘上没有任何痕迹（哨兵目录不存在）")

    # =====================================================================
    # 1) 新会话：多轮对话（接缝工具 + 审批），事件逐条落盘
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 新会话，多轮对话（FakeLLM 剧本驱动；工具面 = 接缝注册表；LocalFS）")
    services = ServiceContainer()
    services.register(FS_CAPABILITY, LocalFS(WORKSPACE))
    story_fs = services.resolve(FS_CAPABILITY)  # ← 显式 resolve（本阶段机制）
    story_registry = build_filesystem_registry(story_fs)

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
        tool_registry=story_registry,
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
        tool_registry=build_filesystem_registry(LocalFS(WORKSPACE)),
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
    assert replayed[: len(online_history)] == online_history, "前期在线历史与重放前缀不一致！"
    extended = plain(project(resumed_ctx.session.events))
    assert extended == plain(resumed.history), "扩展投影与在线历史不一致！"
    print("   ✓ 一致（消息历史完全由日志投影而来，且前期快照是重放前缀）")
    print("   ✓ 扩展投影（最近一次 system/message 生效）与在线历史一致")
    latest = latest_prompt_trace(resumed_ctx.session.events)
    assert rebuild_text(latest) == effective_system_prompt(resumed_ctx.session.events)
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（提示词机制在接缝工具下照常工作）")

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

    # 顺带展示一个"结构化错误"长什么样（工具层翻译 FsError 的证据）
    missing = story_registry.resolve("read_file").func(path="notes/none.txt")
    assert missing["code"] == FS_NOT_FOUND
    not_a_file = story_registry.resolve("read_file").func(path="notes")
    assert not_a_file["code"] == FS_NOT_A_FILE
    print("\n结构化错误示例（工具层翻译 FsError → 给模型的结果）：")
    print(f"   {missing}")
    print(f"   {not_a_file}")

    print("\n全部步骤完成。可再试：python chat.py --fake --fs memory --ask 「写个文件」")


if __name__ == "__main__":
    main()
