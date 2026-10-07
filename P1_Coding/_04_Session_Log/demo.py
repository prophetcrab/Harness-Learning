"""_04_Session_Log 的入口脚本：一次把"事件溯源"的完整故事演一遍。

运行方式（在 _04_Session_Log 目录下）：

    python demo.py            # 全离线：不用 key，用 FakeLLM 驱动，故事固定
    python demo.py --keep     # 保留 sessions/ 演示目录（默认每次清空重建）

故事线（对应学习计划 M3 的验收）：
    1. 新会话，跑一个 turn（模型 → 工具 → 模型），事件逐条落盘
    2. 打印日志：能看到 user/message、assistant/message、tool/result 等事件
    3. 模拟崩溃：往 jsonl 尾部写半行 JSON（相当于 kill -9 打断写入）
    4. 恢复：重新打开会话，坏尾被自动截断修复，已确认事件不丢
    5. 继续对话：resume 后历史完整、turn 编号接续
    6. 同构断言：**重放日志得到的历史 == 在线产生的历史**
    7. 分叉：从某个 seq 派生出新会话，原会话不受影响
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from llm_seam import FakeLLM, build_default_toolbox, text_reply, tool_call_reply
from runner import Runner, fork_session, load_messages, open_session
from session import JsonlStore

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
SESSIONS = HERE / "sessions"
SYSTEM_PROMPT = "你是一名严谨的中文助手，涉及算术必须调用 calculate。"


def plain(messages):
    return [
        (m.role, m.content, tuple((c.id, c.name) for c in m.tool_calls), m.tool_call_id)
        for m in messages
    ]


def main() -> None:
    keep = "--keep" in sys.argv
    if SESSIONS.exists() and not keep:
        shutil.rmtree(SESSIONS)  # 每次干净重演（--keep 可保留）

    store = JsonlStore(SESSIONS)
    toolbox = build_default_toolbox()

    # ---- 1) 新会话 + 一个 turn ----
    print("=" * 64)
    print("1) 新会话，跑一个 turn（模型申请 calculate → 执行 → 最终回答）")
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52"),
        ]
    )
    session, _ = open_session("demo", store, system_prompt=SYSTEM_PROMPT)
    runner = Runner(session, provider, toolbox.execute, toolbox.specs())
    result = runner.send("帮我算 1234*56.78")
    online = plain(runner.history)
    print(f"   最终回答：{result.final_text}")
    print(f"   在线历史角色序列：{[m.role for m in runner.history]}")

    # ---- 2) 打印日志 ----
    print("\n" + "=" * 64)
    print("2) 会话日志（append-only 事件序列）")
    for event in session.events:
        print(f"   #{event.seq:>2} {event.type}")
    print(f"   日志文件：{store.path('demo')}")

    # ---- 3) 模拟崩溃 ----
    print("\n" + "=" * 64)
    print("3) 模拟崩溃：往 jsonl 尾部写半行 JSON（进程被强杀的那一瞬间）")
    path = store.path("demo")
    path.write_bytes(path.read_bytes() + b'{"seq": 99, "type": "user/mess')
    print(f"   已在尾部注入半截事件；文件现有 {path.stat().st_size} 字节")

    # ---- 4) 恢复（自动修坏尾）----
    print("\n" + "=" * 64)
    print("4) 恢复：重新打开会话")
    session2, report = open_session("demo", store, system_prompt=SYSTEM_PROMPT)
    print(f"   修复报告：repaired={report.repaired}，丢弃 {report.dropped_bytes} 字节")
    print(f"   恢复后事件数：{len(session2.events)}（半截事件已被丢弃）")

    # ---- 5) 继续对话 ----
    print("\n" + "=" * 64)
    print("5) 继续对话（resume）：历史完整、turn 编号接续")
    provider2 = FakeLLM([text_reply("不客气！")])
    runner2 = Runner(session2, provider2, toolbox.execute, toolbox.specs())
    print(f"   resume 时的历史角色序列：{[m.role for m in runner2.history]}")
    second = runner2.send("谢谢")
    print(f"   第二个 turn：turn={second.turn}，回答={second.final_text!r}")

    # ---- 6) 同构断言 ----
    print("\n" + "=" * 64)
    print("6) 同构断言：重放日志得到的历史 == 在线产生的历史")
    replayed = plain(load_messages("demo", store))
    assert replayed == plain(runner2.history), "重放历史与在线历史不一致！"
    print("   ✓ 一致（消息历史完全由日志投影而来）")

    # ---- 7) 分叉 ----
    print("\n" + "=" * 64)
    print("7) 分叉：从第 4 条事件处派生一个新会话")
    forked = fork_session(store, "demo", "demo-fork", upto_seq=4)
    print(f"   demo-fork 事件：{[e.type for e in forked.events]}")
    print(f"   原会话 demo 仍是 {len(session2.events)} 条（未被改动）")
    print(f"   现有会话：{store.list_sessions()}")
    print("\n全部步骤完成。可用 `python cli.py list` 查看落盘结果。")


if __name__ == "__main__":
    main()
