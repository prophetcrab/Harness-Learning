"""_08_Profile_Layers 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线，FakeLLM 驱动，故事固定（每次清空 demo_run/ 重演）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = M1–M3 的组装验收（基线行为回归，改动任何底层后都该保持全绿）：
    1. 新会话，多轮对话：算数（工具）、写文件（工具 + 审批）、闲聊
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
from pathlib import Path

from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.mini import MiniHarness
from harness.runner import fork_session, load_messages
from harness.session import JsonlStore
from harness.tools import ScriptedApprover
from harness.tools.approval import ApprovalDecision

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
WORKSPACE = DEMO_RUN / "demo_workspace"
SYSTEM_PROMPT = (
    "你是一名严谨的中文助手，可以调用 calculate 做算术、用 read_file/write_file/list_files 读写文件。"
)

NOTE = "harness 学习笔记\n- M1 主循环\n- M2 工具管线\n- M3 会话日志"


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
    # 1) 新会话：多轮对话（工具 + 审批 + 闲聊），事件逐条落盘
    # =====================================================================
    print("=" * 68)
    print("1) 新会话，多轮对话（FakeLLM 剧本驱动）")
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
    harness, report = MiniHarness.open(
        "demo",
        provider=provider,
        root=SESSIONS,
        workspace=WORKSPACE,
        system_prompt=SYSTEM_PROMPT,
        approval=approval,
        on_event=lambda kind, payload: None,  # demo 自己控制打印节奏
    )
    for question in ["帮我算 1234*56.78", "把要点记到 notes/todo.txt", "记住了吗？"]:
        result = harness.send(question)
        print(f"   你：{question}")
        print(f"   助手：{result.final_text}")

    online_history = plain(harness.history)  # 供第 5 步同构对照
    provider.assert_all_consumed()
    approval.assert_all_consumed()
    print(f"   在线历史角色序列：{[m.role for m in harness.history]}")

    # =====================================================================
    # 2) 打印会话日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("2) 会话日志（append-only 事件序列，逐条落盘）")
    for event in harness.session.events:
        print(f"   #{event.seq:>2} {event.type}")
    print(f"   日志文件：{JsonlStore(SESSIONS).path('demo')}")

    # =====================================================================
    # 3) 模拟"退出"：丢掉进程内对象，只剩磁盘上的日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("3) 退出：丢弃进程内的 harness 对象（历史只留在磁盘日志里）")
    del harness
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
    resumed, report2 = MiniHarness.open(
        "demo",
        provider=provider2,
        root=SESSIONS,
        workspace=WORKSPACE,
        system_prompt=SYSTEM_PROMPT,
        approval=ScriptedApprover([]),
        on_event=lambda kind, payload: None,
    )
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
    print("   ✓ 一致（消息历史完全由日志投影而来，且前期快照是重放前缀）")

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

    print("\n全部步骤完成。可再试 CLI：python -m harness list / show demo")


if __name__ == "__main__":
    main()
