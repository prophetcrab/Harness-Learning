"""chat.py —— P3 基础包的对话入口（配置 = 插件选择）。

用法：

    python chat.py                      # 全离线：FakeLLM + 内存文件 + 剧本命令
    python chat.py --ask "帮我算 2+3"    # 离线跑一个 turn 后退出
    python chat.py --realtime           # 真实：DeepSeek + 真实磁盘 + 真实进程
    python chat.py --realtime --ask "..."       # 同上，一次提问
    python chat.py --no-approve         # 需审批的操作自动放行（默认：终端 y/n）

会话内命令：/plugins 插件台账；/services 服务清单；/tools 工具面；
            /history 历史；/exit 退出（整体卸载插件，可见回卷）。

与 P2 的差别：装配不再是"读 YAML 配置树"，而是**直接列插件**——P3 基础包
把"完全插件化"作为地基，配置分层（P2 `_08`/`_09`）留待后续在这个底座上重做。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from components.tools import ApprovalPolicy, PromptApprover
from plugins import TOOLS_SERVICE, Booted, boot_offline, boot_realtime

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # 项目根（.env 所在处）
DEFAULT_ROOT = HERE / "sessions"
DEFAULT_WORKSPACE = HERE / "demo_workspace" / "ws"


def build_approval(args: argparse.Namespace) -> ApprovalPolicy | None:
    """审批策略：默认终端 y/n；--no-approve 自动放行；--deny 全部拒绝。"""
    if args.no_approve:
        from components.tools import AutoApprove

        return AutoApprove()
    if args.deny:
        from components.tools import AutoDeny

        return AutoDeny("命令行指定了 --deny")
    if not sys.stdin.isatty():
        # 非交互终端问不到人：fail-closed（拒绝一切需审批操作）。
        from components.tools import AutoDeny

        return AutoDeny("非交互终端：无人可审批（fail-closed）")
    return PromptApprover()


def print_event(kind: str, payload: dict) -> None:
    """主循环事件 → 人看的进度（保持精简：只在关键时刻出声）。"""
    if kind == "step_response":
        calls = payload["tool_calls"]
        if calls:
            names = ", ".join(f"{call['name']}({call['arguments']})" for call in calls)
            print(f"  → 模型申请调用：{names}")
        elif payload["content"]:
            print(f"  · 模型说明：{payload['content'][:60]}")
    elif kind == "tool_result":
        mark = "✗" if payload["is_error"] else "✓"
        text = str(payload["result"])
        print(f"  {mark} {payload['name']} → {text[:100]}")


def show_ledger(booted: Booted) -> None:
    """打印插件台账与工具面（"完全插件化"的可见性）。"""
    print("\n插件台账：")
    for info in booted.ctx.plugins:
        state = info.status if info.status != "active" else "active"
        inject = f"  inject={list(info.inject)}" if info.inject else ""
        print(f"  - {info.name}  [{state}]{inject}")
    print(f"服务：{booted.ctx.services}")
    tools = booted.ctx.require(TOOLS_SERVICE)
    print(f"工具面（{len(tools.names)}）：{', '.join(tools.names)}")


def repl(booted: Booted, session_id: str) -> int:
    """交互循环：一条输入 = 一个 turn。"""
    opened = booted.agent.open(session_id, on_event=print_event)
    if opened.repaired:
        print("[提示] 上次会话被强杀，日志尾部已自动修复。")
    print(f"会话 {session_id}（{'恢复' if opened.resumed else '新建'}）；/exit 退出，/plugins 看台账。")
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/exit":
            return 0
        if text == "/plugins":
            show_ledger(booted)
            continue
        if text == "/services":
            print(f"  {booted.ctx.services}")
            continue
        if text == "/tools":
            print(f"  {booted.ctx.require(TOOLS_SERVICE).names}")
            continue
        if text == "/history":
            for message in opened.messages:
                head = (message.content or "").replace("\n", " ")[:70]
                print(f"  [{message.role}] {head}")
            continue
        result = booted.agent.ask(text, session_id=session_id)
        print(f"助手：{result.final_text}")
        if result.status != "done":
            print(f"  （turn 以 {result.status} 结束，共 {len(result.steps)} 步）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chat.py", description="P3 插件化 mini harness：插件列表即配置"
    )
    parser.add_argument("--ask", help="跑一个 turn 后退出（不给则进入交互）")
    parser.add_argument("--session", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="工作区目录")
    parser.add_argument("--realtime", action="store_true", help="真实组合（DeepSeek + 磁盘 + 进程）")
    parser.add_argument("--fs", choices=("local", "jail"), default="local", help="--realtime 的文件实现")
    parser.add_argument("--shell", choices=("local", "none"), default="local", help="--realtime 是否装 shell")
    parser.add_argument("--no-approve", action="store_true", help="需审批操作自动放行")
    parser.add_argument("--deny", action="store_true", help="禁止一切需审批操作")
    args = parser.parse_args(argv)

    approval = build_approval(args)
    if args.realtime:
        booted = boot_realtime(
            root=args.root,
            workspace=args.workspace,
            project_root=PROJECT_ROOT,
            approval=approval,
            fs=args.fs,
            shell=args.shell,
        )
        mode = f"真实组合（DeepSeek + {args.fs} + shell={args.shell}）"
    else:
        booted = boot_offline(root=args.root, workspace=args.workspace, approval=approval)
        mode = "离线组合（FakeLLM + MemoryFS + 剧本命令）"
    print(f"装配完成：{mode}")
    show_ledger(booted)

    try:
        if args.ask is not None:
            result = booted.agent.ask(args.ask, session_id=args.session)
            print(f"\n助手：{result.final_text}")
            return 0 if result.status == "done" else 1
        return repl(booted, args.session)
    finally:
        booted.close()  # 整体卸载：一切登记物回卷（退出前看得见）
        print(f"[退出] 插件已全部卸载；服务：{booted.ctx.services}")


if __name__ == "__main__":
    raise SystemExit(main())
