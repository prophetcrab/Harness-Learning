"""_06_Subprocess_Seam 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + M1–M3 的组装验收（基线行为回归）：

    0a. [M5 _06 新增] 命令接缝：同一套请求在 LocalSubprocess（真实进程）与
        ScriptedSubprocess（剧本回放）下得到同一种结果词汇（退出码/stdout/stderr/超时）
    0b. 结果词汇的三条渲染：正常 / 非零退出 / 超时——工具层把它们翻成
        给模型的结构化结果（与文件错误同构）
    0c. 消费侧约束：工作目录与期限在"组装时"显式解析，模型只给 command；
        输出超限被截断（防日志爆炸）
    1. 新会话（fs=local + shell=LocalSubprocess），多轮对话：算数 / 真实执行
       一条命令（审批）/ 写文件（审批）/ 闲聊
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
    FS_NOT_FOUND,
    SHELL_NONZERO_EXIT,
    SHELL_TIMEOUT,
    SUBPROCESS_CAPABILITY,
    CommandResult,
    LocalFS,
    LocalSubprocess,
    MemoryFS,
    ScriptedSubprocess,
    ServiceContainer,
    SubprocessService,
    build_toolbox,
)

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志\n- M4 提示词机制\n- M5 能力接缝（fs + shell）"


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


def err_str(result: dict) -> str:
    """压缩展示一个工具错误（demo 打印用）。"""
    return f"code={result.get('code')}  error={result.get('error')!r}"


def main() -> None:
    keep = "--keep" in sys.argv
    if DEMO_RUN.exists() and not keep:
        shutil.rmtree(DEMO_RUN)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # =====================================================================
    # 0a) [本阶段新增] 命令接缝：两个 provider，同一种结果词汇
    # =====================================================================
    print("=" * 68)
    print("0a) SubprocessService 接缝：LocalSubprocess（真跑） vs ScriptedSubprocess（回放）")
    local_svc = LocalSubprocess()
    scripted_svc = ScriptedSubprocess(
        [
            CommandResult(0, "hello from script\n"),
            CommandResult(7, "", "script failure\n"),
        ]
    )
    print(f"   两者都满足协议：{isinstance(local_svc, SubprocessService)} / "
          f"{isinstance(scripted_svc, SubprocessService)}")

    real = local_svc.run(f'{sys.executable} -c "print(6*7)"')
    fake = scripted_svc.run("print(6*7)")
    print(f"   真实执行：exit={real.exit_code} stdout={real.stdout.strip()!r}")
    print(f"   剧本回放：exit={fake.exit_code} stdout={fake.stdout.strip()!r}")
    failed = scripted_svc.run("failing-command")
    print(f"   非零结局同样是'结果'：exit={failed.exit_code} stderr={failed.stderr.strip()!r}")
    scripted_svc.assert_all_consumed()
    print("   → 返回类型相同（CommandResult）；换 provider，上层无感。")

    # =====================================================================
    # 0b) 结果词汇的三条渲染：正常 / 非零退出 / 超时
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) 三种结局的渲染（工具层翻译，与文件错误同构）")
    scripted_svc = ScriptedSubprocess(
        [
            CommandResult(0, "all good\n"),
            CommandResult(1, "", "not found\n"),
            CommandResult(None, "partial...", "", timed_out=True),
        ]
    )
    registry = build_toolbox(MemoryFS(), scripted_svc, workspace="/demo/ws", shell_timeout=5)
    shell = registry.resolve("shell").func

    normal = shell(command="echo all good")
    nonzero = shell(command="missing-cmd")
    timeout = shell(command="sleep forever")
    print(f"   正常结束  → {normal}")
    print(f"   非零退出  → {err_str(nonzero)}")
    print(f"   超时被杀  → {err_str(timeout)}")
    assert nonzero["code"] == SHELL_NONZERO_EXIT and timeout["code"] == SHELL_TIMEOUT
    assert timeout["exit_code"] is None and "partial..." in timeout["stdout"]
    assert scripted_svc.request_count == 3
    scripted_svc.assert_all_consumed()

    # =====================================================================
    # 0c) 消费侧约束：cwd / 期限 / 输出上限在组装时解析
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 消费侧约束：请求里能带什么，由组装时决定（模型只给 command）")
    scripted_svc = ScriptedSubprocess([CommandResult(0, "x" * 30000)])
    registry = build_toolbox(
        MemoryFS(),
        scripted_svc,
        workspace="/demo/ws",
        shell_timeout=12,
        shell_max_output_chars=500,
    )
    out = registry.resolve("shell").func(command="big-output")
    print(f"   输出超限截断：len={len(out['stdout'])}（原始 30000）→ ...{out['stdout'][-26:]!r}")
    request = scripted_svc.requests[0]
    print(f"   请求记录：cwd={request.cwd!r} timeout={request.timeout!r}（组装时显式解析）")
    assert request.cwd == str(Path("/demo/ws"))  # 规范化后传入（Windows 会转成 \demo\ws）
    assert request.timeout == 12
    scripted_svc.assert_all_consumed()

    # =====================================================================
    # 1) 新会话：真实 shell + 工具（审批），事件逐条落盘
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 新会话，多轮对话（FakeLLM 剧本；fs=LocalFS；shell=LocalSubprocess）")
    services = ServiceContainer()
    services.register(FS_CAPABILITY, LocalFS(WORKSPACE))
    services.register(SUBPROCESS_CAPABILITY, LocalSubprocess())
    story_fs = services.resolve(FS_CAPABILITY)  # ← 显式 resolve
    story_shell = services.resolve(SUBPROCESS_CAPABILITY)
    story_registry = build_toolbox(story_fs, story_shell, workspace=WORKSPACE, shell_timeout=15)
    print(f"   工具面：{story_registry.names}（shell 已接入）")

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
    # ★ 命令的结果确实进了历史（stdout 里就有 2）
    shell_step = next(
        step for step in ctx.harness.messages if step.role == "tool" and "2" in step.content
    )
    assert "2" in shell_step.content
    print("   ✓ 两次审批（命令 + 写入）都用在了正确的位置；命令输出进了模型历史")
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
    # 3) 模拟"退出"
    # =====================================================================
    print("\n" + "=" * 68)
    print("3) 退出：丢弃进程内的 harness 对象（历史只留在磁盘日志里）")
    del ctx
    print("   进程内已无任何会话状态。")

    # =====================================================================
    # 4) resume
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
    resumed_ctx = open_context_harness(
        "demo",
        provider=provider2,
        root=SESSIONS,
        workspace=WORKSPACE,
        approval=ScriptedApprover([]),
        context_source=story_context,
        tool_registry=build_toolbox(
            LocalFS(WORKSPACE), LocalSubprocess(), workspace=WORKSPACE, shell_timeout=15
        ),
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
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（提示词机制在 shell 工具下照常工作）")

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
    print("\n结构化错误示例（与文件错误同构——同样的 error + code 两键）：")
    print(f"   文件类：{missing}")
    print(f"   命令类：{nonzero}")

    print("\n全部步骤完成。可再试：python chat.py --fake --ask 「用 shell 看看目录」")


if __name__ == "__main__":
    main()
