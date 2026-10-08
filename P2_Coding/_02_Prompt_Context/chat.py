"""chat.py —— 每 step 渲染的系统提示词：和 harness 做一场真实对话。

这是本阶段（`_02_Prompt_Context`）的**交互式入口**，回答一个问题：
"提示词里的 {{cwd}}/{{platform}}/{{time}} 在真实对话里怎么'活'起来？"

它做的事：
1. 用顶层 `prompt/` 包装配系统提示词（默认 role/tools/env/style 四节；env 一节带
   {{cwd}}/{{platform}}/{{time}} 占位符；再叠加 `chat` 作用域追加 conversation 一节）；
2. 用顶层 `context/` 包接线（open_context_harness）：**每 step** 重新采样运行时上下文
   → 重新渲染 → 变了就作为 system/message 事件记进日志；模型每一步看到的就是
   当时的最新渲染（由 RuntimePromptProvider 改写请求）；
3. 启动时打印装配明细（模板 + 当下渲染结果）；会话中 `/prompt` 会**现场重新渲染**
   （时间会变）——这是"每 step 渲染"最直观的证据。

用法（在 _02_Prompt_Context 目录下）：

    python chat.py                       # 真实 API 交互对话（读取项目根 .env 的 key）
    python chat.py --fake                # 离线剧本对话（不需要 key）
    python chat.py --session s1          # 指定会话 id；同名即"恢复继续"
    python chat.py --ask "现在几点？"     # 一条问题跑一个 turn 后退出
    python chat.py --no-scope            # 只看基础四节，不叠加 chat 作用域
    python chat.py --no-approve          # 写文件自动放行（跳过 y/n 审批）

会话内命令：/prompt 重新渲染并打印装配明细；/context 看当前生效的提示词（日志重建）；
/history 看历史角色序列；/exit 退出。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 本阶段新增的机制（顶层模块）
from context import (
    collect_runtime_context,
    effective_system_prompt,
    open_context_harness,
)

# 基线的装配/提供者/工具（未改动）
from harness.env import build_deepseek_provider
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.session import JsonlStore
from harness.tools import AutoApprove, PromptApprover
from harness.tools.approval import AutoDeny
from prompt import PromptAssembler, Section, default_registry

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # _02_Prompt_Context -> 项目根（读取 .env）
DEFAULT_ROOT = HERE / "sessions"
DEFAULT_WORKSPACE = HERE / "demo_workspace"


# =========================================================================
# 1) ★ 本阶段机制：section + 变量 → 每 step 渲染系统提示词 ★
# =========================================================================


def build_assembler(*, with_scope: bool = True) -> tuple[PromptAssembler, PromptAssembler]:
    """构造装配器，返回 (基础装配器, 生效装配器)。

    - 基础装配器：内置默认四节（role / tools / env / style；env 带变量占位符）；
    - 生效装配器：在其上叠加名为 `chat` 的作用域，**追加**一节 `conversation`。

    返回两个是为了在启动时对比"叠加前 / 叠加后"，直观看到作用域的效果。
    """
    registry = default_registry()
    base = PromptAssembler(registry)

    if not with_scope:
        return base, base

    scope = registry.scoped("chat")
    scope.register(
        Section(
            "conversation",
            "这是一次多轮对话：请保持上下文连贯；信息不足时可以先追问用户澄清。",
        )
    )
    return base, PromptAssembler(scope)


def show_prompt(assembler: PromptAssembler, title: str, variables: dict[str, str]) -> None:
    """打印装配明细：逐节模板（name / source / 内容）+ 按 variables 的渲染结果。

    模板行里保留 {{name}} 原文（展示"这一节引用了什么"）；渲染结果里占位符已被
    替换成当下取值（展示"这一刻模型会看到什么"）。
    """
    print(f"\n【{title}】共 {len(assembler.parts())} 节：")
    for index, section in enumerate(assembler.parts(), start=1):
        head = f"  {index}. {section.name}"
        if section.title:
            head += f"（{section.title}）"
        head += f"  来源={section.source}"
        print(head)
        for line in section.content.splitlines() or [""]:
            print(f"       模板 | {line}")
    print(f"  —— 渲染结果（按当下变量插值）——\n{assembler.assemble(variables)}")


# =========================================================================
# 2) provider / 审批（与基线 cli 一致的构造方式）
# =========================================================================


def build_provider(args: argparse.Namespace):
    """真实 API 优先；--fake 用离线剧本。"""
    if args.fake:
        script = [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52。（离线剧本）"),
            text_reply("（离线剧本：这是一句通用回答，真实对话请去掉 --fake）"),
            text_reply("（离线剧本已给出通用回答）"),
        ]
        return FakeLLM(script), "FakeLLM（离线剧本，不需要 key）"
    return build_deepseek_provider(PROJECT_ROOT), "DeepSeekProvider（真实 API）"


def build_approval(args: argparse.Namespace):
    if args.no_approve:
        return AutoApprove()
    if args.deny:
        return AutoDeny("--deny：本会话禁止一切需审批操作")
    return PromptApprover()


def _update_count(ctx) -> int:
    """当前日志里的 system/message 条数（用于提示"本 turn 更新了几次渲染"）。"""
    return sum(1 for event in ctx.session.events if event.type == "system/message")


# =========================================================================
# 3) 对话循环
# =========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chat.py", description="每 step 渲染的系统提示词：真实对话"
    )
    parser.add_argument("--session", "-s", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="文件工具工作区")
    parser.add_argument("--fake", action="store_true", help="离线剧本（不需要 API key）")
    parser.add_argument("--ask", default=None, help="一条问题跑一个 turn 后退出")
    parser.add_argument("--no-scope", action="store_true", help="不叠加 chat 作用域，只用基础四节")
    parser.add_argument("--no-approve", action="store_true", help="写文件自动放行（跳过审批）")
    parser.add_argument("--deny", action="store_true", help="禁止一切需审批操作")
    args = parser.parse_args(argv)

    # ---- 装配系统提示词（本阶段机制）----
    workspace = Path(args.workspace)
    base, active = build_assembler(with_scope=not args.no_scope)
    print("=" * 68)
    print("系统提示词由 section 装配、按运行时上下文渲染（本阶段机制）：")
    show_prompt(base, "基础 section（模板）", collect_runtime_context(workspace).variables())
    if not args.no_scope:
        show_prompt(
            active,
            "叠加 chat 作用域后（追加了 conversation 一节）",
            collect_runtime_context(workspace).variables(),
        )

    # ---- 开一场会话：每 step 渲染交给 RuntimePromptProvider ----
    inner, mode = build_provider(args)
    ctx = open_context_harness(
        args.session,
        provider=inner,
        root=args.root,
        workspace=args.workspace,
        assembler=active,  # ★ 渲染用的就是上面装配出的 section
        context_source=lambda: collect_runtime_context(workspace),  # ★ 每步重新采样
        approval=build_approval(args),
    )
    harness = ctx.harness

    print("\n" + "=" * 68)
    print(f"provider：{mode}")
    print(f"会话：{harness.session.session_id}    已有 turn：{harness.session.last_turn_number}")
    print(f"工具：{[spec.name for spec in harness.tools]}")
    if ctx.report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {ctx.report.dropped_bytes} 字节）")
    print("每 step 都会重新渲染系统提示词；渲染有变化时写入 system/message 事件。")
    print("会话内命令：/prompt 现场重新渲染；/context 当前生效文本；/history 历史；/exit 退出。")

    if args.ask is not None:
        return _ask_once(ctx, args)

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
            print("  /prompt 装配明细；/context 当前生效；/history 历史；/exit 退出。")
            continue
        if text == "/prompt":
            # ★ 现场重新渲染一次：变量在"现在"取值（时间会变）
            show_prompt(active, "重新渲染（时间按此刻取值）", collect_runtime_context(workspace).variables())
            continue
        if text == "/context":
            current = effective_system_prompt(ctx.session.events)
            count = _update_count(ctx)
            print(f"  日志里当前生效的系统提示词（system/message 共 {count} 条）：")
            print(f"  {current}")
            continue
        if text == "/history":
            print(f"  历史：{[m.role for m in harness.messages]}")
            continue

        before = _update_count(ctx)
        try:
            result = harness.send(text)
        except AssertionError:  # FakeLLM 剧本用完（离线模式常见）
            if args.fake:
                print("（离线剧本已用完；想看真实对话请去掉 --fake）")
                continue
            raise
        after = _update_count(ctx)
        print("\n助手>", result.final_text)
        if after > before:
            print(f"（本 turn 系统提示词更新 {after - before} 次：新增 system/message 事件）")
        print(f"（本 turn status={result.status}，step {len(result.steps)} 个）")

    print(f"\n会话已保存：{JsonlStore(Path(args.root)).path(args.session)}")
    print(f"下次继续：python chat.py --session {args.session}")
    return 0


def _ask_once(ctx, args: argparse.Namespace) -> int:
    """--ask：一条问题跑一个 turn。"""
    before = _update_count(ctx)
    result = ctx.harness.send(args.ask)
    after = _update_count(ctx)
    print(f"\n你> {args.ask}")
    print("助手>", result.final_text)
    if after > before:
        print(f"（系统提示词更新 {after - before} 次：新增 system/message 事件）")
    print(f"（status={result.status}，step {len(result.steps)} 个）")
    print(f"日志：{JsonlStore(Path(args.root)).path(args.session)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
