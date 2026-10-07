"""mini harness 的命令行入口：对话 / 恢复 / 会话管理。

用法（在 P3_Coding 目录下；也可 `python -m harness <子命令>`）：

    python -m harness chat                            # 真实 API，交互对话（新建/恢复 default 会话）
    python -m harness chat --session s1               # 指定会话 id；同名即"恢复继续"
    python -m harness chat --fake                     # 离线剧本（不需要 key）
    python -m harness chat --no-approve               # 写文件自动放行（跳过 y/n 审批）
    python -m harness chat --search                   # 额外启用 web_search 工具（需网络）
    python -m harness run "帮我算 1234*56.78" --fake   # 一条输入跑一个 turn 后退出
    python -m harness list                            # 列会话
    python -m harness show <session_id>               # 看某个会话的事件日志
    python -m harness fork <src> <new> [--upto N]     # 从某个会话分叉

约定：`chat`/`run` 对不存在的 session_id 是"创建"，对已存在的是"恢复"——同一条路径。
会话日志默认落在 ./sessions/，文件工具的工作区默认是 ./workspace/（可用 --root / --workspace 改）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness.env import build_deepseek_provider
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.mini import MiniHarness
from harness.runner import fork_session
from harness.session import JsonlStore
from harness.tools import AutoApprove, PromptApprover
from harness.tools.approval import AutoDeny

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # _05_Mini_Harness/../ = 项目根
DEFAULT_ROOT = HERE.parent / "sessions"
DEFAULT_WORKSPACE = HERE.parent / "demo_workspace"


# =========================================================================
# provider 构造：唯一的"选供应商"落点
# =========================================================================


def _fake_chat_script() -> list:
    """离线剧本：够覆盖几轮"工具调用 + 回答"的演示（用完会明确提示）。"""
    return [
        tool_call_reply("calculate", {"expression": "1234*56.78"}),
        text_reply("1234 × 56.78 = 70066.52。（来自 FakeLLM 剧本）"),
        text_reply("这是一个离线剧本回答：我不会真的联网或调模型。"),
        text_reply("（离线剧本已给出通用回答）"),
    ]


def build_provider(args: argparse.Namespace):
    if args.fake:
        return FakeLLM(_fake_chat_script()), "FakeLLM（离线剧本，不需要 key）"
    return build_deepseek_provider(PROJECT_ROOT), "DeepSeekProvider（真实 API）"


def build_approval(args: argparse.Namespace):
    if args.no_approve:
        return AutoApprove()
    if args.deny:
        return AutoDeny("--deny：本会话禁止一切需审批操作")
    return PromptApprover()


# =========================================================================
# 事件展示：把循环的每一轮打印成人能读的日志（观察者，不落库）
# =========================================================================


def show_event(kind: str, payload: dict) -> None:
    if kind == "turn_start":
        print(f"\n── turn {payload['turn']} 开始 ──")
    elif kind == "step_request":
        print(
            f"  [step {payload['step']}] 请求模型："
            f"{payload['message_count']} 条消息 + {payload['tool_count']} 个工具"
        )
    elif kind == "step_response":
        calls = payload["tool_calls"]
        if calls:
            print(f"  [step {payload['step']}] ← 申请调用 {[c['name'] for c in calls]}")
        else:
            print(f"  [step {payload['step']}] ← 最终回答")
    elif kind == "tool_result":
        mark = "✗ 失败" if payload["is_error"] else "✓ 成功"
        print(f"           → {payload['name']}({payload['arguments']}) {mark}：{payload['result']}")


# =========================================================================
# 子命令
# =========================================================================


def _open(args: argparse.Namespace, provider) -> tuple[MiniHarness, object]:
    return MiniHarness.open(
        args.session,
        provider=provider,
        root=args.root,
        workspace=args.workspace,
        approval=build_approval(args),
        include_search=args.search,
        max_steps=args.max_steps,
        on_event=show_event,
    )


def cmd_chat(args: argparse.Namespace) -> int:
    provider, mode = build_provider(args)
    harness, report = _open(args, provider)

    print(f"provider：{mode}")
    print(f"会话：{harness.session.session_id}    已有 turn：{harness.session.last_turn_number}")
    print(f"工具：{[spec.name for spec in harness.tools]}")
    print(f"工作区：{Path(args.workspace).resolve()}")
    if report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {report.dropped_bytes} 字节）")
    print("输入内容开始对话；/history 看历史，/exit 退出（退出后历史已落盘，可再进来恢复）。")

    while True:
        try:
            line = input("\n你> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        if text == "/help":
            print("  输入任意文本对话；/history 查看历史角色序列；/exit 退出。")
            continue
        if text == "/history":
            print(f"  历史：{[m.role for m in harness.messages]}")
            continue

        try:
            result = harness.send(text)
        except AssertionError:  # FakeLLM 剧本用完（离线模式常见）
            if args.fake:
                print("（离线剧本已用完；真实对话请去掉 --fake）")
                continue
            raise
        print("\n助手>", result.final_text)
        print(f"（本 turn status={result.status}，step {len(result.steps)} 个）")

    print(f"\n会话已保存：{JsonlStore(Path(args.root)).path(args.session)}")
    print(f"下次继续：python -m harness chat --session {args.session}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    provider, mode = build_provider(args)
    harness, report = _open(args, provider)
    if report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {report.dropped_bytes} 字节）")
    print(f"provider：{mode}    会话：{args.session}    已有 turn：{harness.session.last_turn_number}")
    result = harness.send(args.question)
    print("\n════════ 最终回答 ════════")
    print(result.final_text)
    print(f"\nstatus={result.status}    turn={result.turn}    step 数={len(result.steps)}")
    print(f"日志：{JsonlStore(Path(args.root)).path(args.session)}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = JsonlStore(Path(args.root))
    ids = store.list_sessions()
    if not ids:
        print(f"（{store.root} 下暂无会话）")
        return 0
    print(f"会话（root={store.root}）：")
    for sid in ids:
        events, _ = store.load(sid)
        turns = sum(1 for e in events if e.type == "turn/end")
        tail = events[-1].type if events else "空"
        print(f"  {sid}    事件 {len(events)} 条，turn {turns} 个，末事件 {tail}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = JsonlStore(Path(args.root))
    events, report = store.load(args.session)
    if report.repaired:
        print(f"[修复] {report.reason}（丢弃 {report.dropped_bytes} 字节）")
    if not events:
        print(f"会话不存在或为空：{args.session}")
        return 1
    print(f"会话 {args.session}（{len(events)} 条事件）")
    for event in events:
        detail = {
            k: v
            for k, v in event.data.items()
            if k in ("content", "name", "call_id", "status", "turn", "step", "final_text")
        }
        print(f"  #{event.seq:>3} {event.type:<18} {detail}")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    store = JsonlStore(Path(args.root))
    forked = fork_session(store, args.src, args.new, upto_seq=args.upto)
    print(f"已从 {args.src} 分叉出 {args.new}（{len(forked.events)} 条事件）")
    print(f"日志：{store.path(args.new)}")
    return 0


# =========================================================================
# 参数解析
# =========================================================================


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", "-s", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="文件工具的工作区目录")
    parser.add_argument("--fake", action="store_true", help="离线剧本（不需要 API key）")
    parser.add_argument("--no-approve", action="store_true", help="写文件自动放行（跳过审批）")
    parser.add_argument("--deny", action="store_true", help="本会话禁止一切需审批操作")
    parser.add_argument("--search", action="store_true", help="额外启用 web_search 工具（需网络）")
    parser.add_argument("--max-steps", type=int, default=8, help="每个 turn 的 step 上限")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness", description="mini harness：对话 / 恢复 / 会话管理"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_chat = sub.add_parser("chat", help="交互式对话（可恢复）")
    _add_common(p_chat)
    p_chat.set_defaults(func=cmd_chat)

    p_run = sub.add_parser("run", help="一条输入跑一个 turn")
    p_run.add_argument("question", help="要问的问题")
    _add_common(p_run)
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="列出会话")
    p_list.add_argument("--root", default=str(DEFAULT_ROOT))
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看某个会话的事件日志")
    p_show.add_argument("session")
    p_show.add_argument("--root", default=str(DEFAULT_ROOT))
    p_show.set_defaults(func=cmd_show)

    p_fork = sub.add_parser("fork", help="从已有会话分叉")
    p_fork.add_argument("src")
    p_fork.add_argument("new")
    p_fork.add_argument("--upto", type=int, default=None, help="只拷贝 seq <= N 的前缀")
    p_fork.add_argument("--root", default=str(DEFAULT_ROOT))
    p_fork.set_defaults(func=cmd_fork)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
