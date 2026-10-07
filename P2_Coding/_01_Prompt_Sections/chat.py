"""chat.py —— 用「section 装配出的系统提示词」和 harness 做一场真实对话。

这是本阶段（`_01_Prompt_Sections`）的**交互式入口**，回答一个问题：
"新机制在真实对话里长什么样？"

它做的事：
1. 用顶层 `prompt/` 包把系统提示词**装配**出来（默认 role/tools/style 三节，
   再叠加一个名为 `chat` 的作用域追加一节 `conversation`）——这正是本阶段的机制；
2. 启动时把装配明细（每节的 name / source / 内容）打印出来，让你看见提示词是"拼"出来的；
3. 把装配出的那段文本交给基线的 `MiniHarness.open(system_prompt=...)` 开启会话；
4. 进入多轮对话循环（默认调用**真实 DeepSeek API**；`--fake` 用离线剧本）。

**代码组织（P2 约定）**：本文件是阶段主目录顶层的**新增入口**，`harness/` 基线未改动——
装配点就在本文件里（assembler.assemble() 的结果传给 MiniHarness.open）。

用法（在 _01_Prompt_Sections 目录下）：

    python chat.py                      # 真实 API 交互对话（读取项目根 .env 的 key）
    python chat.py --fake               # 离线剧本对话（不需要 key，剧本有限）
    python chat.py --session s1         # 指定会话 id；同名即"恢复继续"
    python chat.py --no-scope           # 只看基础三节，不叠加 chat 作用域
    python chat.py --no-approve         # 写文件自动放行（跳过 y/n 审批）

会话内命令：/prompt 再打印一次装配明细；/history 看历史角色序列；/exit 退出。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 基线的装配/提供者/工具（未改动）
from harness.env import build_deepseek_provider
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.mini import MiniHarness
from harness.session import JsonlStore
from harness.tools import AutoApprove, PromptApprover
from harness.tools.approval import AutoDeny

# 本阶段新增的机制（顶层模块）
from prompt import PromptAssembler, Section, SectionRegistry, default_sections

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # _01_Prompt_Sections -> 项目根
DEFAULT_ROOT = HERE / "sessions"
DEFAULT_WORKSPACE = HERE / "demo_workspace"


# =========================================================================
# 1) ★ 本阶段机制：用 section 装配系统提示词 ★
# =========================================================================


def build_assembler(*, with_scope: bool = True) -> tuple[PromptAssembler, PromptAssembler]:
    """构造装配器，返回 (基础装配器, 生效装配器)。

    - 基础装配器：只用内置默认三节（role / tools / style）；
    - 生效装配器：在其上叠加一个名为 `chat` 的作用域，**追加**一节 `conversation`。

    返回两个是为了在启动时对比"叠加前 / 叠加后"，直观看到作用域的效果。
    """
    registry = SectionRegistry()
    for section in default_sections():
        registry.register(section)
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


def show_prompt(assembler: PromptAssembler, title: str) -> None:
    """把装配明细打印出来：逐节显示 name / source / 内容。"""
    print(f"\n【{title}】共 {len(assembler.parts())} 节：")
    for index, section in enumerate(assembler.parts(), start=1):
        head = f"  {index}. {section.name}"
        if section.title:
            head += f"（{section.title}）"
        head += f"  来源={section.source}"
        print(head)
        for line in section.content.splitlines() or [""]:
            print(f"       {line}")
    print(f"  —— 拼接结果 ——\n{assembler.assemble()}")


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
    parser = argparse.ArgumentParser(prog="chat.py", description="用 section 装配的提示词做真实对话")
    parser.add_argument("--session", "-s", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="文件工具工作区")
    parser.add_argument("--fake", action="store_true", help="离线剧本（不需要 API key）")
    parser.add_argument("--no-scope", action="store_true", help="不叠加 chat 作用域，只用基础三节")
    parser.add_argument("--no-approve", action="store_true", help="写文件自动放行（跳过审批）")
    parser.add_argument("--deny", action="store_true", help="禁止一切需审批操作")
    args = parser.parse_args(argv)

    # ---- 装配系统提示词（本阶段机制）----
    base, active = build_assembler(with_scope=not args.no_scope)
    print("=" * 68)
    print("系统提示词由 section 装配（本阶段机制）：")
    show_prompt(base, "基础 section")
    if not args.no_scope:
        show_prompt(active, "叠加 chat 作用域后（追加了 conversation 一节）")

    # ---- 用装配结果开一场会话 ----
    provider, mode = build_provider(args)
    harness, report = MiniHarness.open(
        args.session,
        provider=provider,
        root=args.root,
        workspace=args.workspace,
        system_prompt=active.assemble(),  # ★ 装配产物交给基线开会话
        approval=build_approval(args),
    )

    print("\n" + "=" * 68)
    print(f"provider：{mode}")
    print(f"会话：{harness.session.session_id}    已有 turn：{harness.session.last_turn_number}")
    print(f"工具：{[spec.name for spec in harness.tools]}")
    if report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {report.dropped_bytes} 字节）")
    print("输入内容对话；/prompt 看装配明细，/history 看历史，/exit 退出。")

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
            print("  输入任意文本对话；/prompt 装配明细；/history 历史角色序列；/exit 退出。")
            continue
        if text == "/prompt":
            show_prompt(active, "当前生效的系统提示词")
            continue
        if text == "/history":
            print(f"  历史：{[m.role for m in harness.messages]}")
            continue

        try:
            result = harness.send(text)
        except AssertionError:  # FakeLLM 剧本用完（离线模式常见）
            if args.fake:
                print("（离线剧本已用完；想看真实对话请去掉 --fake）")
                continue
            raise
        print("\n助手>", result.final_text)
        print(f"（本 turn status={result.status}，step {len(result.steps)} 个）")

    print(f"\n会话已保存：{JsonlStore(Path(args.root)).path(args.session)}")
    print(f"下次继续：python chat.py --session {args.session}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
