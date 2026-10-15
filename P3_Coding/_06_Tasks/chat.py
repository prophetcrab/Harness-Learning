"""chat.py —— P3 基础包的对话入口（配置 = 插件选择）。

用法：

    python chat.py                      # 全离线：离线模型 + 内存文件 + 剧本命令
    python chat.py --ask "帮我算 2+3"    # 离线跑一个 turn 后退出
    python chat.py --realtime           # 真实：DeepSeek + 真实磁盘 + 真实进程
    python chat.py --realtime --ask "..."       # 同上，一次提问
    python chat.py --no-approve         # 需审批的操作自动放行（默认：终端 y/n）
    python chat.py --web                # ★ 界面插件：起网页，Ctrl+C 停服退出
    python chat.py --web --port 0       # 端口交给系统分配（URL 打印在控制台）
    python chat.py --rpc                # ★ RPC 服务（stdio JSON-RPC；EOF 退出）
    python chat.py --rpc --listen 8771  # RPC 服务同时监听 TCP（多客户端）

会话内命令：/plugins 插件台账；/services 服务清单；/tools 工具面；
            /history 历史；/compact 手动压缩上下文（折到最后一轮之前）；
            /todo 任务清单（看/加/勾/删，不花模型的钱）；
            /web 起/停界面插件；/exit 退出（整体卸载，可见回卷）。

与 P2 的差别：装配不再是"读 YAML 配置树"，而是**直接列插件**——P3 基础包
把"完全插件化"作为地基，配置分层（P2 `_08`/`_09`）留待后续在这个底座上重做。
**界面与 RPC 都是插件**（plugins/webui.py、plugins/rpc.py）：装载即起、卸载即停。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from components.context.tokens import DEFAULT_CONTEXT_WINDOW
from components.todo import run_todo_command
from components.tools import ApprovalPolicy, PromptApprover
from plugins import (
    TODOS_SERVICE,
    TOOLS_SERVICE,
    WEBUI_SERVICE,
    Booted,
    boot_offline,
    boot_realtime,
)

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # 项目根（.env 所在处）
DEFAULT_ROOT = HERE / "sessions"
DEFAULT_WORKSPACE = HERE / "demo_workspace" / "ws"


def build_approval(args: argparse.Namespace) -> ApprovalPolicy | None:
    """审批策略：Web + 自扩展模式交给浏览器卡片；否则终端 y/n；两个开关强制放行/拒绝。

    返回 None = "不要在装配时定死策略"——agent 会退回用 ctx 里的 `approval`
    服务（装了 web-approval 就是浏览器审批；没装则 fail-closed 拒绝）。
    """
    if args.no_approve:
        from components.tools import AutoApprove

        return AutoApprove()
    if args.deny:
        from components.tools import AutoDeny

        return AutoDeny("命令行指定了 --deny")
    if args.web and args.self_tools:
        # 界面 + 自扩展：审批点在浏览器里（web-approval 插件已随之装载）。
        return None
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


def show_ledger(booted: Booted, *, stream=None) -> None:
    """打印插件台账与工具面（"完全插件化"的可见性）。

    `stream` 默认 stdout；**RPC 模式必须传 stderr**——stdout 是协议线，
    一个字节都不许混（P2 `_10` 定下的纪律，这里靠参数落实）。
    """
    stream = stream if stream is not None else sys.stdout
    print("\n插件台账：", file=stream)
    for info in booted.ctx.plugins:
        state = info.status if info.status != "active" else "active"
        inject = f"  inject={list(info.inject)}" if info.inject else ""
        print(f"  - {info.name}  [{state}]{inject}", file=stream)
    print(f"服务：{booted.ctx.services}", file=stream)
    tools = booted.ctx.require(TOOLS_SERVICE)
    print(f"工具面（{len(tools.names)}）：{', '.join(tools.names)}", file=stream)


def start_webui(booted: Booted, *, port: int) -> str:
    """装载界面插件并返回它的 URL（装载即起服务）。"""
    from plugins import webui_plugin

    booted.ctx.load_plugin(webui_plugin(port=port))
    return booted.ctx.require(WEBUI_SERVICE).url


def stop_webui(booted: Booted) -> None:
    """卸载界面插件（卸载即停服、端口释放）。"""
    booted.ctx.unload_plugin("webui")


def serve_web(booted: Booted, *, port: int, open_browser: bool) -> int:
    """--web 模式：起界面插件 → 打印地址 → 阻塞到 Ctrl+C → 停服。"""
    url = start_webui(booted, port=port)
    print(f"\n界面插件已装载：{url}")
    print("  （插件台账见页面左栏；对话与事件流实时更新；Ctrl+C 停服退出）")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        while True:
            input()  # 回车/Ctrl+C 都用来退出；Ctrl+C 走 except
    except (KeyboardInterrupt, EOFError):
        print()
    stop_webui(booted)
    print("[webui] 已卸载：服务停止、端口释放")
    return 0


def serve_rpc(booted: Booted, *, listen: int | None) -> int:
    """--rpc 模式：装载 RPC 插件（可选 TCP），把 stdin/stdout 当传输跑 stdio。

    - stdout 是**协议线**：只出帧；banner 一律走 stderr；
    - EOF（客户端断开）即正常收尾；TCP 模式（--listen）下服务持续到 EOF。
    """
    from plugins import RPC_SERVICE, rpc_plugin, serve_stdio

    booted.ctx.load_plugin(rpc_plugin(listen=listen))
    service = booted.ctx.require(RPC_SERVICE)
    if listen is not None:
        host, port = service.address or ("127.0.0.1", listen)
        print(f"[rpc] TCP 正在监听 {host}:{port}（stdio 同时可用）", file=sys.stderr)
    print("[rpc] 从 stdin 读 JSON-RPC 帧，向 stdout 写帧；EOF 退出。", file=sys.stderr)
    try:
        serve_stdio(service)
    except KeyboardInterrupt:
        print(file=sys.stderr)
    finally:
        booted.ctx.unload_plugin("rpc")
        print("[rpc] 已卸载：服务停止（TCP 端口释放）", file=sys.stderr)
    return 0


def repl(booted: Booted, session_id: str, *, web_port: int = 8766) -> int:
    """交互循环：一条输入 = 一个 turn；`/web` 现场装卸界面插件。"""
    opened = booted.agent.open(session_id, on_event=print_event)
    if opened.repaired:
        print("[提示] 上次会话被强杀，日志尾部已自动修复。")
    print(f"会话 {session_id}（{'恢复' if opened.resumed else '新建'}）；/exit 退出，/plugins 看台账，/compact 压缩，/todo 清单。")
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
        if text == "/todo" or text.startswith("/todo "):
            # `/todo` 默认是**人命令**：直接改清单，不走 turn、不花模型的钱。
            # 唯一例外是 `/todo run`——它要求把清单交给模型执行（那次要花钱）；
            # 构件层只负责拼出提示词，由这里决定怎么发。
            todos = booted.ctx.get(TODOS_SERVICE)
            if todos is None:
                print("  [todo] todo 插件未装载（清单服务不可用）")
                continue
            result = run_todo_command(todos.store(session_id), text[len("/todo"):])
            if result.run_prompt is not None:
                print("  [todo] 把清单交给模型执行…")
                turn = booted.agent.ask(result.run_prompt, session_id=session_id)
                print(f"助手：{turn.final_text}")
                print(f"  [todo] {todos.view(session_id)['summary']}")
                continue
            for line in result.text.splitlines():
                print(f"  [todo] {line}")
            continue
        if text == "/compact":
            # 手动压缩（`_05` 的 `/compact`）：跳过阈值、保住最后一轮。
            # 失败要把原因打出来（手动动作不静默；自动路径才 fail-soft）。
            try:
                outcome = booted.agent.compact(session_id)
            except Exception as exc:
                print(f"  [compact] 压缩失败：{exc}")
                continue
            if outcome is None:
                print("  [compact] 没有可压缩的历史（只有当前一轮）。")
            else:
                print(
                    f"  [compact] 已折叠 {outcome.folded} 条消息（{outcome.before_tokens} → "
                    f"{outcome.after_tokens} tokens，摘要约 {outcome.summary_tokens}）"
                )
            continue
        if text == "/web":
            if "webui" in booted.ctx.active_plugins:
                stop_webui(booted)
                print("  [webui] 已卸载（服务停止、端口释放）")
            else:
                print(f"  [webui] {start_webui(booted, port=web_port)}")
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
    parser.add_argument("--web", action="store_true", help="装载界面插件并服务到 Ctrl+C")
    parser.add_argument("--port", type=int, default=8766, help="界面端口（0 = 系统分配空闲端口）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--rpc", action="store_true", help="装载 RPC 插件：stdio JSON-RPC 服务")
    parser.add_argument("--listen", type=int, default=None, help="--rpc 同时监听 TCP 端口（0 = 系统分配）")
    parser.add_argument(
        "--self-tools", action="store_true",
        help="★ 装上自扩展工具（_01 机制）：模型可装卸白名单插件（需审批）",
    )
    parser.add_argument(
        "--max-steps", type=int, default=None,
        help="单个 turn 的步数上限（默认不限——长程任务不会被截断；"
             "给了数字则超限时以 max_steps 状态收尾）",
    )
    parser.add_argument(
        "--token-budget", type=int, default=None,
        help="单个 turn 的 token 上限（`_03`：超限以 budget_exceeded 收尾；默认不限）",
    )
    parser.add_argument(
        "--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW,
        help="模型上下文窗口（`_04` 仪表分母 + `_05` 压缩阈值基数；默认 1M）",
    )
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
            self_tools=args.self_tools,
            max_steps=args.max_steps,
            token_budget=args.token_budget,
            context_window=args.context_window,
        )
        mode = f"真实组合（DeepSeek + {args.fs} + shell={args.shell}）"
    else:
        booted = boot_offline(
            root=args.root, workspace=args.workspace, approval=approval,
            self_tools=args.self_tools, max_steps=args.max_steps,
            token_budget=args.token_budget, context_window=args.context_window,
        )
        mode = "离线组合（离线模型 + MemoryFS + 剧本命令）"
    if args.self_tools:
        mode += " + 自扩展工具"
    mode += (f"｜步数上限 {args.max_steps}" if args.max_steps else "｜步数不限")
    # RPC 模式：所有人类可读输出走 stderr（stdout 只出协议帧）。
    banner_stream = sys.stderr if args.rpc else sys.stdout
    print(f"装配完成：{mode}", file=banner_stream)
    show_ledger(booted, stream=banner_stream)

    try:
        if args.rpc:
            return serve_rpc(booted, listen=args.listen)
        if args.web:
            return serve_web(booted, port=args.port, open_browser=not args.no_open)
        if args.ask is not None:
            result = booted.agent.ask(args.ask, session_id=args.session)
            print(f"\n助手：{result.final_text}")
            return 0 if result.status == "done" else 1
        return repl(booted, args.session, web_port=args.port)
    finally:
        booted.close()  # 整体卸载：一切登记物回卷（退出前看得见）
        print(f"[退出] 插件已全部卸载；服务：{booted.ctx.services}", file=banner_stream)


if __name__ == "__main__":
    raise SystemExit(main())
