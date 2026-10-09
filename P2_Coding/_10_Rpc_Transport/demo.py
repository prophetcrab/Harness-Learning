"""_10_Rpc_Transport 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线（FakeLLM / MemoryFS / 剧本命令）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + 前序机制回归：

    0a. [M7 _10 新增] 换行 JSON-RPC 协议层：帧解析/构造的词汇与错误码
    0b. 服务化：HarnessService 经 LineTransport 收发——握手（init 前门禁）/
        session.prompt 跑 turn（会话缓存、turn 接续）/ 落盘 / 审批 fail-closed
    0c. 前序机制回归：配置装配（_08/_09）仍是服务的底座（banner 用 dump 树）
    1. 主故事：boot dev profile 跑多轮对话；结束后整体卸载看回卷
    2. 打印会话日志；3. 模拟退出；4. resume；5. 同构断言；6. 分叉
"""

from __future__ import annotations

import io
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    load_profile,
    render_tree,
)
from context import (
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
    LLM_CAPABILITY,
    TOOLBOX_CAPABILITY,
    boot_tree,
)
from server import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    HarnessService,
    LineTransport,
    Request,
    error_frame,
    notification_frame,
    parse_line,
    result_frame,
)

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
PROFILES = HERE / "profiles"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)


def plain(messages):
    """把消息压成可比较的元组序列（同构断言用）。"""
    return [
        (m.role, m.content, tuple((c.id, c.name) for c in m.tool_calls), m.tool_call_id)
        for m in messages
    ]


def _write_bad(path: Path, text: str) -> Path:
    """往临时文件写一段（demo 里构造"坏配置"用）。"""
    path.write_text(text, encoding="utf-8")
    return path


def rpc(service: HarnessService, lines: list[str]) -> list[str]:
    """把一串请求行喂给服务，返回响应帧列表（StringIO 驱动——与 stdio 同一路径）。"""
    reader = io.StringIO("\n".join(lines) + "\n")
    writer = io.StringIO()
    LineTransport(reader, writer, service).serve_forever()
    return writer.getvalue().splitlines()


def main() -> None:
    keep = "--keep" in sys.argv
    if DEMO_RUN.exists() and not keep:
        shutil.rmtree(DEMO_RUN)
    DEMO_RUN.mkdir(parents=True, exist_ok=True)

    # =====================================================================
    # 0a) [本阶段新增] 协议层：帧的词汇
    # =====================================================================
    print("=" * 68)
    print("0a) 换行 JSON-RPC 协议层：一行文本 ↔ 一种帧")
    samples = [
        ('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}', "请求"),
        ('{"jsonrpc":"2.0","method":"session.prompt","params":{"text":"hi"}}', "通知（无 id）"),
        ("这行不是 JSON", "坏帧"),
    ]
    for raw, label in samples:
        try:
            frame = parse_line(raw)
            kind = "Request" if isinstance(frame, Request) else "Notification"
            print(f"   [{label}] {kind}: method={frame.method!r}")
        except Exception as exc:  # RpcError
            print(f"   [{label}] {exc.code}: {exc}")
    print(f"   构造（golden 形状）：{result_frame(1, {'ok': True})}")
    print(f"   {error_frame(1, INVALID_PARAMS, '参数不对')}")
    print(f"   {notification_frame('session.event', {'seq': 5})}")

    # =====================================================================
    # 0b) 服务化：握手 / prompt / 门禁 / 落盘 / 审批
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) HarnessService 经 LineTransport（StringIO 驱动，与 stdio 同一路径）")
    rpc_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    rpc_ws = DEMO_RUN / "rpc_ws"
    rpc_ws.mkdir(parents=True, exist_ok=True)
    service = HarnessService(rpc_ctx, root=DEMO_RUN / "rpc_sessions", workspace=rpc_ws)

    responses = rpc(
        service,
        [
            # 未握手先调用 → 门禁拦下
            '{"jsonrpc":"2.0","id":9,"method":"session.prompt","params":{"text":"x"}}',
            # 握手
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            # 两次 prompt：同名会话 → turn 接续
            '{"jsonrpc":"2.0","id":2,"method":"session.prompt","params":{"session":"s1","text":"帮我算 1234*56.78"}}',
            '{"jsonrpc":"2.0","id":3,"method":"session.prompt","params":{"session":"s1","text":"再跑一条命令"}}',
            # 参数错误
            '{"jsonrpc":"2.0","id":4,"method":"session.prompt","params":{}}',
            # 未知方法
            '{"jsonrpc":"2.0","id":5,"method":"no.such.method","params":{}}',
        ],
    )
    for line in responses:
        print(f"   ← {line}")
    assert '"code": -32002' in responses[0]                       # 未握手门禁
    assert '"turn": 1' in responses[2] and '"turn": 2' in responses[3]  # 会话接续
    assert f'"code": {INVALID_PARAMS}' in responses[4] and f'"code": {METHOD_NOT_FOUND}' in responses[5]
    print(f"   会话缓存：{service.open_sessions}；落盘："
          f"{(DEMO_RUN / 'rpc_sessions' / 's1' / 'session.jsonl').is_file()}")
    print("   ✓ 握手门禁 / 会话接续 / 参数与方法的错误码 / 日志落盘")

    # 审批 fail-closed：服务端无人应答 → 需审批的工具默认被拒。
    # （FakeLLM 剧本是共享队列：s1 的两次 prompt 已消耗前两幕，这里拿到的是写文件那幕。）
    responses = rpc(
        service,
        [
            '{"jsonrpc":"2.0","id":6,"method":"session.prompt","params":{"session":"s2","text":"继续"}}',
        ],
    )
    assert '"status": "done"' in responses[0]
    events, _ = JsonlStore(DEMO_RUN / "rpc_sessions").load("s2")
    denied = [e for e in events if e.type == "tool/result" and e.data["is_error"]]
    assert denied and denied[0].data["name"] == "write_file", "fail-closed 应该拒绝需审批的写文件"
    print(f"   ✓ 审批 fail-closed：s2 里 {denied[0].data['name']} 被拒 —— "
          f"{denied[0].data['result']['error'][:30]}……")

    # =====================================================================
    # 0c) 前序机制回归：配置装配仍是服务的底座
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 前序机制回归：服务由配置装配（_08/_09）boot 而来")
    print(render_tree(load_profile("dev", profiles_dir=PROFILES), title="服务使用的配置（dev）")[:300] + "……")
    dev = load_profile("dev", profiles_dir=PROFILES)
    assert dev.source_of("llm", "name") == "dev.yaml"   # 来源追踪（_09）仍在
    print(f"   ✓ 来源台账仍在：llm.name ← {dev.source_of('llm', 'name')}")

    # =====================================================================
    # 1) 主故事：boot dev profile 跑多轮对话
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 主故事：boot dev profile（FakeLLM + MemoryFS + 剧本命令）")
    story_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    llm = story_ctx.require(LLM_CAPABILITY)
    registry = story_ctx.require(TOOLBOX_CAPABILITY)
    print(f"   插件台账：{story_ctx.effect_names}")
    print(f"   工具面：{registry.names}")

    workspace = DEMO_RUN / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    approval = ScriptedApprover(
        [
            ApprovalDecision(True, "demo：批准执行命令（剧本，不真跑）"),
            ApprovalDecision(True, "demo：批准写入"),
        ]
    )
    ctx = open_context_harness(
        "demo",
        provider=llm,
        root=SESSIONS,
        workspace=workspace,
        approval=approval,
        context_source=lambda: collect_runtime_context(
            workspace, now=lambda: STORY_MOMENT, platform_name="DemoOS"
        ),
        tool_registry=registry,
    )
    for question in [
        "帮我算 1234*56.78",
        "用 shell 看一眼目录",
        "把要点记到 notes/todo.txt",
    ]:
        result = ctx.harness.send(question)
        print(f"   你：{question}")
        print(f"   助手：{result.final_text}")

    online_history = plain(ctx.harness.history)
    approval.assert_all_consumed()
    print("   ✓ 命令与写入两次审批都用在了正确的位置（dev：全离线，零真实副作用）")
    assert list(workspace.iterdir()) == []  # dev 的 fs 是 MemoryFS：磁盘无痕迹
    print("   ✓ MemoryFS 生效：工作区目录里没有任何文件（写只进内存）")

    print(f"   整体卸载前槽位：{story_ctx.slots}")
    for name in reversed(story_ctx.effect_names):
        story_ctx.unload(name)
    print(f"   整体卸载后槽位：{story_ctx.slots}")
    assert story_ctx.slots == []

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
    # 4) resume（重新 boot 一份配置）
    # =====================================================================
    print("\n" + "=" * 68)
    print("4) resume：重新加载配置、重新打开会话（对旧 id 是恢复 —— 同一段代码）")
    resume_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    resume_script = FakeLLM(
        [
            tool_call_reply("list_files", {"path": "."}),
            text_reply("内存里记着笔记（MemoryFS 不跨进程）。"),
        ]
    )
    resumed_harness_ctx = open_context_harness(
        "demo",
        provider=resume_script,
        root=SESSIONS,
        workspace=workspace,
        approval=ScriptedApprover([]),
        context_source=lambda: collect_runtime_context(
            workspace, now=lambda: STORY_MOMENT, platform_name="DemoOS"
        ),
        tool_registry=resume_ctx.require(TOOLBOX_CAPABILITY),
    )
    resumed = resumed_harness_ctx.harness
    print(f"   resume 时的历史角色序列：{[m.role for m in resumed.messages]}")
    print(f"   接续 turn 编号：{resumed.session.last_turn_number}")
    r4 = resumed.send("工作区里有哪些文件？")
    print(f"   你：工作区里有哪些文件？\n   助手：{r4.final_text}")
    resume_script.assert_all_consumed()

    # =====================================================================
    # 5) 同构断言
    # =====================================================================
    print("\n" + "=" * 68)
    print("5) 同构断言：重放日志得到的历史 == 在线产生的历史")
    replayed = plain(load_messages("demo", JsonlStore(SESSIONS)))
    assert replayed == plain(resumed.history), "重放历史与在线历史不一致！"
    assert replayed[: len(online_history)] == online_history, "前期在线历史与重放前缀不一致！"
    extended = plain(project(resumed_harness_ctx.session.events))
    assert extended == plain(resumed.history), "扩展投影与在线历史不一致！"
    print("   ✓ 一致（消息历史完全由日志投影而来，且前期快照是重放前缀）")
    latest = latest_prompt_trace(resumed_harness_ctx.session.events)
    assert rebuild_text(latest) == effective_system_prompt(resumed_harness_ctx.session.events)
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（提示词机制在配置装配下照常工作）")

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

    print("\n全部步骤完成。可再试：python chat.py --dump-config / python chat.py --profile prod")


if __name__ == "__main__":
    main()
