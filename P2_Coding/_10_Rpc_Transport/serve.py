"""serve.py —— stdio JSON-RPC 服务入口：harness 作为常驻服务运行。

这是本阶段（`_10_Rpc_Transport`）的**招牌入口**。启动后：

1. 按 profile 配置 boot 装配（`_07`–`_09` 的机制：插件/effect/配置分层）；
2. 起 [LineTransport]：从 **stdin** 逐行读 JSON-RPC 帧、把响应写 **stdout**；
3. banner 与所有人类可读的日志写 **stderr**——stdout 是协议线，一个字节都不许混。

用法（在 _10_Rpc_Transport 目录下）：

    python serve.py                        # dev profile（离线：FakeLLM + MemoryFS）
    python serve.py --profile prod         # prod profile（DeepSeek + 本地）
    python serve.py --auto-approve         # 放开审批（默认 fail-closed 拒绝）

客户端示例（换行 JSON，一行一帧）：

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
    {"jsonrpc":"2.0","id":2,"method":"session.prompt","params":{"session":"s1","text":"帮我算 2+3"}}

    # 手工试跑（Git Bash）：
    # printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' | python serve.py

退出：stdin 关闭（客户端断开）即正常收尾，退出码 0。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import ConfigError, load_profile
from providers import boot_tree
from server import HarnessService, LineTransport

HERE = Path(__file__).resolve().parent
PROFILES_DIR = HERE / "profiles"
DEFAULT_ROOT = HERE / "sessions"
DEFAULT_WORKSPACE = HERE / "demo_workspace" / "ws"


def build_service(args: argparse.Namespace) -> HarnessService:
    """按配置 boot 装配并构造服务（配置错误 → 带定位的 ConfigError 上抛）。"""
    tree = load_profile(args.profile, profiles_dir=PROFILES_DIR)
    plug_ctx = boot_tree(tree, stage_root=HERE)

    approval = None
    if args.auto_approve:
        from harness.tools import AutoApprove

        approval = AutoApprove()
    return HarnessService(
        plug_ctx,
        root=Path(args.root),
        workspace=Path(args.workspace),
        approval=approval,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="serve.py", description="stdio 换行 JSON-RPC 服务（initialize / session.prompt）"
    )
    parser.add_argument("--profile", default="dev", help="profile 名（profiles/<name>.yaml）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="会话工作区")
    parser.add_argument(
        "--auto-approve", action="store_true",
        help="放开审批（默认 fail-closed：服务端无人工应答方，拒绝一切需审批操作）",
    )
    args = parser.parse_args(argv)

    try:
        service = build_service(args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    # banner 走 stderr（stdout 是协议线）。
    print(
        f"[serve] profile={args.profile} 会话日志根={args.root} "
        f"审批={'放开' if args.auto_approve else 'fail-closed'}",
        file=sys.stderr,
    )
    print("[serve] 从 stdin 读 JSON-RPC 帧，向 stdout 写响应；EOF 退出。", file=sys.stderr)

    transport = LineTransport(sys.stdin, sys.stdout, service)
    transport.serve_forever()
    print("[serve] stdin 已关闭，服务正常退出。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
