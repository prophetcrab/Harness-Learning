"""chat.py —— 装配单（trace）与 --dump-prompt：提示词怎么拼的、从哪来、能不能重建。

这是本阶段（`_03_Prompt_Trace`）的**交互式入口**，回答三个问题：

1. 这份提示词由哪些 section 组成、每节**来自哪里**（builtin / chat 作用域）？
2. 每节引用了哪些变量、当时的取值是什么？
3. 事后能不能由日志里的记录**原样重建**？（rebuild_text(日志) == 记录文本）

机制（沿用 `_02`）：每 step 重新渲染；渲染有变化时把**装配单**随 `system/message`
事件一起写进日志，开场那份在 `session/start` 里（接线见 `context.wiring`）。

用法（在 _03_Prompt_Trace 目录下）：

    python chat.py                             # 真实 API 交互对话（读项目根 .env）
    python chat.py --fake                      # 离线剧本对话（不需要 key）
    python chat.py --ask "现在几点？"           # 一条问题跑一个 turn 后退出
    python chat.py --ask "..." --dump-prompt   # ★ 附带打印装配单与重建校验
    python chat.py --session s1                # 指定会话 id（同名即"恢复继续"）
    python chat.py --no-scope                  # 只看基础四节，不叠加 chat 作用域

会话内命令：/prompt 当前生效装配单（来自日志，含重建校验）；/context 当前生效文本；
/history 历史角色序列；/exit 退出。
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
from prompt import PromptAssembler, Section, default_registry, rebuild_text

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # _03_Prompt_Trace -> 项目根（读取 .env）
DEFAULT_ROOT = HERE / "sessions"
DEFAULT_WORKSPACE = HERE / "demo_workspace"


# =========================================================================
# 1) ★ 本阶段机制：装配单展示 + 重建校验 ★
# =========================================================================


def build_assembler(*, with_scope: bool = True) -> tuple[PromptAssembler, PromptAssembler]:
    """构造装配器，返回 (基础装配器, 生效装配器)。

    - 基础装配器：内置默认四节（role / tools / env / style；env 带变量占位符）；
    - 生效装配器：在其上叠加名为 `chat` 的作用域，**追加**一节 `conversation`。
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


def show_trace(trace_data: dict, title: str, source_label: str = "") -> bool:
    """打印一份装配单（来自日志），末尾做"可由日志重建"的校验。

    逐节显示：name / title / **来源** / 引用变量 / 模板（与渲染文本不同时才并排显示）。
    返回重建校验是否通过（rebuild_text(记录) == 记录里的最终文本）。
    """
    sections = trace_data.get("sections", [])
    head = f"\n【{title}】"
    if source_label:
        head += f"（{source_label}）"
    print(f"{head}共 {len(sections)} 节：")
    for index, entry in enumerate(sections, start=1):
        line = f"  {index}. {entry.get('name', '?')}"
        if entry.get("title"):
            line += f"（{entry['title']}）"
        line += f"  来源={entry.get('source', '')}"
        refs = entry.get("referenced") or []
        line += f"  引用变量={'、'.join(refs) if refs else '无'}"
        print(line)
        print(f"       模板 | {entry.get('template', '')}")
        if entry.get("rendered") != entry.get("template"):
            print(f"       渲染 | {entry.get('rendered', '')}")
    variables = trace_data.get("variables", {})
    if variables:
        print("  变量取值：" + " ｜ ".join(f"{key}={value}" for key, value in variables.items()))
    else:
        print("  变量取值：（无）")
    print(f"  —— 渲染结果 ——\n{trace_data.get('text', '')}")
    try:
        rebuilt = rebuild_text(trace_data)
    except ValueError as exc:
        print(f"  ✗ 重建失败：{exc}")
        return False
    if rebuilt == trace_data.get("text"):
        print("  ✓ 重建一致：由日志里的装配单 + 装配器，逐字节还原了当时的提示词")
        return True
    print("  ✗ 重建不一致：由装配单重建的文本与记录文本不同（记录可能被改动过）")
    return False


def latest_trace_with_source(events) -> tuple[dict | None, str]:
    """取当前生效的装配单及其日志位置（如 "system/message #10"）。"""
    for event in reversed(events):
        if event.type in ("system/message", "session/start"):
            trace = event.data.get("prompt_trace")
            if trace is not None:
                return trace, f"{event.type} #{event.seq}"
    return None, ""


def dump_latest(ctx, title: str) -> None:
    """打印"当前生效"的装配单（取自日志，而不是重新渲染）。"""
    trace_data, label = latest_trace_with_source(ctx.session.events)
    if trace_data is None:
        print("\n（日志里没有装配单：这个会话不是由本阶段机制开启的？）")
        return
    show_trace(trace_data, title, f"来自 {label}")


def _update_count(ctx) -> int:
    """当前日志里的 system/message 条数（渲染发生了多少次变化）。"""
    return sum(1 for event in ctx.session.events if event.type == "system/message")


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


# =========================================================================
# 3) 对话循环
# =========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chat.py", description="装配单与重建：看提示词怎么拼的、从哪来"
    )
    parser.add_argument("--session", "-s", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="文件工具工作区")
    parser.add_argument("--fake", action="store_true", help="离线剧本（不需要 API key）")
    parser.add_argument("--ask", default=None, help="一条问题跑一个 turn 后退出")
    parser.add_argument(
        "--dump-prompt",
        action="store_true",
        help="打印装配单（组成/来源/变量/重建校验）——开场与每次变化",
    )
    parser.add_argument("--no-scope", action="store_true", help="不叠加 chat 作用域，只用基础四节")
    parser.add_argument("--no-approve", action="store_true", help="写文件自动放行（跳过审批）")
    parser.add_argument("--deny", action="store_true", help="禁止一切需审批操作")
    args = parser.parse_args(argv)

    # ---- 装配系统提示词（沿用 _01/_02 的机制）----
    workspace = Path(args.workspace)
    _, active = build_assembler(with_scope=not args.no_scope)

    # ---- 开一场会话：每 step 渲染，装配单随事件进日志 ----
    inner, mode = build_provider(args)
    ctx = open_context_harness(
        args.session,
        provider=inner,
        root=args.root,
        workspace=args.workspace,
        assembler=active,
        context_source=lambda: collect_runtime_context(workspace),
        approval=build_approval(args),
    )
    harness = ctx.harness

    print("=" * 68)
    print(f"provider：{mode}")
    print(f"会话：{harness.session.session_id}    已有 turn：{harness.session.last_turn_number}")
    print(f"工具：{[spec.name for spec in harness.tools]}")
    if ctx.report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {ctx.report.dropped_bytes} 字节）")
    print("提示词：由 section 装配、每 step 渲染；装配单随日志保存，可事后重建。")
    print("查看：--dump-prompt / 会话内 /prompt（装配单）；/context（生效文本）；/history；/exit")

    if args.dump_prompt:
        dump_latest(ctx, "开场装配单")

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
            print("  /prompt 装配单（来源 + 重建校验）；/context 生效文本；/history 历史；/exit 退出。")
            continue
        if text == "/prompt":
            dump_latest(ctx, "当前生效的装配单")
            continue
        if text == "/context":
            current = effective_system_prompt(ctx.session.events)
            count = _update_count(ctx)
            print(f"  当前生效的系统提示词（system/message 更新共 {count} 次）：")
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
            print(f"（本 turn 系统提示词更新 {after - before} 次：新增 system/message，携带装配单）")
            if args.dump_prompt:
                dump_latest(ctx, "本 turn 的装配单")
        print(f"（本 turn status={result.status}，step {len(result.steps)} 个）")

    print(f"\n会话已保存：{JsonlStore(Path(args.root)).path(args.session)}")
    print(f"下次继续：python chat.py --session {args.session} --dump-prompt")
    return 0


def _ask_once(ctx, args: argparse.Namespace) -> int:
    """--ask：一条问题跑一个 turn。"""
    before = _update_count(ctx)
    result = ctx.harness.send(args.ask)
    after = _update_count(ctx)
    print(f"\n你> {args.ask}")
    print("助手>", result.final_text)
    if after > before:
        print(f"（系统提示词更新 {after - before} 次：新增 system/message，携带装配单）")
        if args.dump_prompt:
            dump_latest(ctx, "本 turn 的装配单")
    elif args.dump_prompt:
        print("（本 turn 渲染无变化：沿用上面的装配单）")
    print(f"（status={result.status}，step {len(result.steps)} 个）")
    print(f"日志：{JsonlStore(Path(args.root)).path(args.session)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
