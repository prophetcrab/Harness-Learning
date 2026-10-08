"""_05_Workspace_Jail 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + M1–M3 的组装验收（基线行为回归）：

    0a. [M5 _05 新增] 工作区围栏：越界写/编辑被拒（多种路径形态）；
        区内照常；读与列目录不受影响（只拦改动）；jail 可叠在任意机制上
    0b. 单槽服务：重复注册报错、未注册 resolve 报错、显式 resolve（铁律 #5/#6）
    0c. 同一段对话跑三遍（local / jail / memory）：区内操作结果逐字段相同；
        越界写：local 放行（真的写到区外！）vs jail 结构化拒绝
    1. 新会话（provider = jail），多轮对话：算数 / 区内写（审批）/
       **越界写（审批放行也拦）** / 闲聊
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
    FS_SANDBOX_DENIED,
    FsError,
    LocalFS,
    MemoryFS,
    ServiceContainer,
    WorkspaceJailFS,
    build_filesystem_registry,
)

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志\n- M4 提示词机制\n- M5 能力接缝 + 工作区围栏"


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
    # 0a) [本阶段新增] 工作区围栏：越界改动一律拒绝
    # =====================================================================
    print("=" * 68)
    print("0a) WorkspaceJailFS：把「改动」关进工作区（越界 → FS_SANDBOX_DENIED）")
    fence_ws = DEMO_RUN / "fence_ws"
    fence_ws.mkdir(parents=True, exist_ok=True)
    (DEMO_RUN / "outside_secret.txt").write_text("区外的文件（读不受限）", encoding="utf-8")
    jail = WorkspaceJailFS(LocalFS(fence_ws))

    print("   越界形态一律拒绝：")
    for bad in ("../escape.txt", "notes/../../x.txt", "/etc/passwd", "C:/evil.txt", "..\\win.txt"):
        try:
            jail.write_text(bad, "x")
            print(f"     {bad!r} → 未拦住 !!")
        except FsError as exc:
            print(f"     {bad!r} → {exc.code}")
    print("   区内路径照常（含「绕圈但仍在区内」的规范化路径）：")
    jail.write_text("notes/../a.txt", "写进去了")
    print(f"     'notes/../a.txt' → 落盘为 {fence_ws / 'a.txt'}（内容 {jail.read_text('a.txt')!r}）")
    print("   读与列目录不受围栏影响（只拦改动）：")
    outside = jail.read_text("../outside_secret.txt")
    print(f"     读区外文件 → {outside!r}（刻意透传，与 dsh 的 fs-sandbox 边界一致）")
    try:
        jail.edit_text("../outside_secret.txt", "区外", "HACKED")
    except FsError as exc:
        print(f"     edit 区外文件 → {exc.code}（改不动）")

    # 围栏与机制正交：同一道策略可以叠在内存 provider 上
    jail_over_memory = WorkspaceJailFS(MemoryFS())
    jail_over_memory.write_text("m/../b.txt", "内存里的区内文件")
    try:
        jail_over_memory.write_text("../esc.txt", "x")
    except FsError as exc:
        print(f"   jail(MemoryFS)：区内 ok（{jail_over_memory.read_text('b.txt')!r}）；"
              f"越界 → {exc.code}")

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
    # 0c) 同一段对话、三个 provider：区内结果相同；越界命运不同
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 同一段对话跑三遍（local / jail / memory）：区内操作结果逐字段相同")

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

    def tool_results_of(results):
        return [tool_results(r) for r in results]

    three = {
        "local": run_once("seam-local", LocalFS(DEMO_RUN / "fs_local_ws"), DEMO_RUN / "fs_local_ws"),
        "jail": run_once(
            "seam-jail",
            WorkspaceJailFS(LocalFS(DEMO_RUN / "fs_jail_ws")),
            DEMO_RUN / "fs_jail_ws",
        ),
        "memory": run_once("seam-memory", MemoryFS(), DEMO_RUN / "fs_memory_ws"),
    }
    assert tool_results_of(three["local"]) == tool_results_of(three["jail"])
    assert tool_results_of(three["local"]) == tool_results_of(three["memory"])
    print("   ✓ 三份实现：write/read/list 的工具结果逐字段相同（区内操作，策略不插手）")
    print(f"     例：read_file 结果 = {three['memory'][1].steps[0].tool_results[0].result}")
    assert (DEMO_RUN / "fs_local_ws" / "notes" / "demo.txt").is_file()
    assert (DEMO_RUN / "fs_jail_ws" / "notes" / "demo.txt").is_file()
    print("   ✓ local 与 jail 都把文件落到了各自工作区（jail 区内不设障）")

    print("\n   越界写对照（直接过工具层，不经过模型）：")
    escape_ws = DEMO_RUN / "escape_ws"
    local_escape = build_filesystem_registry(LocalFS(escape_ws)).resolve("write_file").func(
        path="../escaped_local.txt", content="逃出去了"
    )
    jail_escape = build_filesystem_registry(WorkspaceJailFS(LocalFS(escape_ws))).resolve(
        "write_file"
    ).func(path="../escaped_jail.txt", content="想逃")
    print(f"     local 工具：{local_escape}")
    print(f"     jail  工具：{jail_escape}")
    assert local_escape.get("written") == "../escaped_local.txt"
    assert (DEMO_RUN / "escaped_local.txt").is_file()  # local 真的写到工作区外
    assert jail_escape["code"] == FS_SANDBOX_DENIED
    assert not (DEMO_RUN / "escaped_jail.txt").exists()  # jail 拦住了
    print("   → 同一句请求：local 放行（文件出现在区外）；jail 结构化拒绝、磁盘无痕迹")

    # =====================================================================
    # 1) 新会话：多轮对话（provider = jail），事件逐条落盘
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 新会话，多轮对话（FakeLLM 剧本驱动；工具面 = 接缝注册表；provider = jail）")
    services = ServiceContainer()
    services.register(FS_CAPABILITY, WorkspaceJailFS(LocalFS(WORKSPACE)))
    story_fs = services.resolve(FS_CAPABILITY)  # ← 显式 resolve
    story_registry = build_filesystem_registry(story_fs)

    provider = FakeLLM(
        [
            # turn 1：算数（走 calculate 工具）
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52。"),
            # turn 2：区内写文件（需审批，批准）
            tool_call_reply("write_file", {"path": "notes/todo.txt", "content": NOTE}),
            text_reply("已把笔记写入 notes/todo.txt。"),
            # turn 3：★ 越界写（审批也批准了——看沙箱拦不拦）
            tool_call_reply("write_file", {"path": "../escape.txt", "content": "越狱尝试"}),
            text_reply("写入被拒绝了：路径越出工作区。我换个位置吧。"),
            # turn 4：闲聊（无工具）
            text_reply("好的，我记住了。"),
        ]
    )
    approval = ScriptedApprover(
        [
            ApprovalDecision(True, "demo：批准区内写入"),
            ApprovalDecision(True, "demo：这次也批准——看沙箱是不是第二道防线"),
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
        "把要点记到 notes/todo.txt",
        "把这行字写到 ../escape.txt",
        "记住了吗？",
    ]:
        result = ctx.harness.send(question)
        print(f"   你：{question}")
        print(f"   助手：{result.final_text}")

    online_history = plain(ctx.harness.history)  # 供第 5 步同构对照
    provider.assert_all_consumed()
    approval.assert_all_consumed()
    # ★ 审批两次都批准了，但越界那次仍被沙箱拦下、磁盘上无痕迹
    assert not (DEMO_RUN / "escape.txt").exists()
    print("   ✓ 审批放行 ≠ 放行：两次写入都批准，越界那次仍被沙箱拒绝（磁盘无痕迹）")
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
            # turn 5：列出工作区文件（证明第 1 步写的文件还在）
            tool_call_reply("list_files", {"path": "."}),
            text_reply("工作区里有 notes/todo.txt。"),
            # turn 6：读回文件内容
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
        tool_registry=build_filesystem_registry(WorkspaceJailFS(LocalFS(WORKSPACE))),
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
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（提示词机制在围栏工具下照常工作）")

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

    # 结构化错误三连（工具层翻译 FsError 的证据；越界错误与其它错误同构）
    missing = story_registry.resolve("read_file").func(path="notes/none.txt")
    assert missing["code"] == FS_NOT_FOUND
    not_a_file = story_registry.resolve("read_file").func(path="notes")
    assert not_a_file["code"] == FS_NOT_A_FILE
    denied = story_registry.resolve("write_file").func(path="../nope.txt", content="x")
    assert denied["code"] == FS_SANDBOX_DENIED
    print("\n结构化错误示例（工具层翻译 FsError → 给模型的结果；三种错误同一结构）：")
    print(f"   {missing}")
    print(f"   {not_a_file}")
    print(f"   {denied}")

    print("\n全部步骤完成。可再试：python chat.py --fake --fs jail --ask 「把 x 写到 ../escape.txt」")


if __name__ == "__main__":
    main()
