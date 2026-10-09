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
import json
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
    HarnessService,
    LineTransport,
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
    # 0a) [本阶段新增] follow = 重放 + 订阅（无缝衔接）
    # =====================================================================
    print("=" * 68)
    print("0a) session.follow：先重放（seq > from_seq），再订阅实时事件")
    rpc_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    rpc_ws = DEMO_RUN / "rpc_ws"
    rpc_ws.mkdir(parents=True, exist_ok=True)
    service = HarnessService(rpc_ctx, root=DEMO_RUN / "follow_sessions", workspace=rpc_ws)

    # 先攒一段历史（一个完成的 turn）
    rpc(service, [
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        '{"jsonrpc":"2.0","id":2,"method":"session.prompt","params":{"session":"f1","text":"第一句"}}',
    ])
    # 再 follow + prompt：一次性观察到 重放 → 响应 → 实时
    lines = rpc(service, [
        '{"jsonrpc":"2.0","id":3,"method":"session.follow","params":{"session":"f1","from_seq":0}}',
        '{"jsonrpc":"2.0","id":4,"method":"session.prompt","params":{"session":"f1","text":"第二句"}}',
    ])
    seqs = [json.loads(line)["params"]["event"]["seq"] for line in lines if '"session.event"' in line]
    follow_at = next(i for i, line in enumerate(lines) if json.loads(line).get("id") == 3)
    replay_count = follow_at  # 响应之前全是重放事件
    live_count = len(seqs) - replay_count  # 其余是实时事件
    print(f"   帧序（{len(lines)} 帧）：重放事件 ×{replay_count} → follow 响应 → 实时事件 ×{live_count} → prompt 响应")
    print(f"   事件 seq：{seqs}（单调递增、不重不漏）")
    print(f"   follow 响应：{json.loads(lines[follow_at])['result']}")
    assert seqs == sorted(set(seqs)) and seqs == list(range(1, len(seqs) + 1))

    # 断线补齐：从某个 seq 之后重连，只重放缺口
    gap_from = 7
    lines = rpc(service, [
        f'{{"jsonrpc":"2.0","id":5,"method":"session.follow","params":{{"session":"f1","from_seq":{gap_from}}}}}',
    ])
    replayed = [json.loads(line)["params"]["event"]["seq"] for line in lines if '"session.event"' in line]
    print(f"\n   断线重连：from_seq={gap_from} → 只重放缺口 {replayed}")
    assert replayed == list(range(gap_from + 1, seqs[-1] + 1))
    print("   ✓ 断线期间的事件全部由服务端重放补齐（'日志是唯一真相'的红利）")

    # =====================================================================
    # 0b) [本阶段新增] 编码工具：edit_file / search_text / find_files
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) 编码工具补齐：edit（精确替换）/ search（内容）/ find（按名）")
    tool_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    registry = tool_ctx.require(TOOLBOX_CAPABILITY)
    print(f"   工具面：{registry.names}（_11 起为基本编码代理补齐）")
    assert {"edit_file", "search_text", "find_files"} <= set(registry.names)

    write = registry.resolve("write_file").func
    edit = registry.resolve("edit_file").func
    search = registry.resolve("search_text").func
    find = registry.resolve("find_files").func

    write(path="src/app.py", content="import os\n# TODO: 修这个问题\nprint(1)\n")
    write(path="tests/test_app.py", content="def test_ok():\n    assert True\n")
    print(f"   edit_file：{edit(path='src/app.py', old='print(1)', new='print(2)')}")
    print(f"   search_text('TODO'): {search(pattern='TODO')['matches']}")
    print(f"   find_files('test_*'): {find(pattern='test_*')['files']}")
    no_match = edit(path="src/app.py", old="不存在的文本", new="x")
    ambiguous = edit(path="src/app.py", old="o", new="x")  # 出现多次 → 拒绝猜
    print(f"   防误改：no_match → {no_match['code']}；ambiguous → {ambiguous['code']}")

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
