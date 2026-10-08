"""chat.py —— 能力接缝对话入口：文件世界 + 命令执行，都是可替换的。

这是本阶段（`_06_Subprocess_Seam`）的**交互式入口**，回答一个问题：
"给'执行命令'也建一条接缝后，模型能做什么？"

它做的事：
1. **文件系统 provider**（`_04`/`_05` 机制）：`--fs local|jail|memory` 三选一，
   经 `ServiceContainer` 登记并 **resolve**（铁律 #6：显式解析优于隐式默认）；
2. **命令执行 provider**（★ 本阶段机制）：`--shell local`（真实进程）或
   `--shell fake`（剧本回放，**不真跑任何命令**）——同一条 `SubprocessService`
   接缝的两个实现，`shell` 工具只认协议。**shell 工具运行时需要审批**。
3. 其余照旧：section 装配 + 每 step 渲染 + 装配单（`_01`–`_03` 的机制）。

用法（在 _06_Subprocess_Seam 目录下）：

    python chat.py                          # 真实 API；fs=local + shell=local（默认）
    python chat.py --shell fake             # 命令走剧本（安全演示：不真跑命令）
    python chat.py --fs jail                # 文件加围栏（越界写被拒）
    python chat.py --fs memory              # 文件只写内存（磁盘无痕迹）
    python chat.py --fake --ask "用 shell 看看当前目录"
    python chat.py --dump-prompt            # 附带装配单（_03 机制）

会话内命令：/prompt 装配单；/context 生效文本；/fs 与 /shell 看当前 provider；/exit。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 前序阶段的机制（顶层模块）
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

# 本阶段新增的机制（顶层模块）
from providers import (
    FS_CAPABILITY,
    SUBPROCESS_CAPABILITY,
    CommandResult,
    LocalFS,
    LocalSubprocess,
    MemoryFS,
    ScriptedSubprocess,
    ServiceContainer,
    WorkspaceJailFS,
    build_toolbox,
)

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # _06_Subprocess_Seam -> 项目根（读取 .env）
DEFAULT_ROOT = HERE / "sessions"
# 默认工作区放深一层：local 模式下"越界写"的落点（../）会落在 demo_workspace/ 里
# （已被 .gitignore 排除），既不污染仓库，又能直观看到"写到工作区外面了"。
DEFAULT_WORKSPACE = HERE / "demo_workspace" / "ws"


# =========================================================================
# 1) ★ 本阶段机制：显式 resolve 一个 FileSystem provider ★
# =========================================================================


def build_file_system(args: argparse.Namespace) -> tuple[object, str]:
    """按 --fs 选择并登记 provider，返回 (fs, 描述)。

    这里是"显式解析"的示范：先 register（单槽：重复会报错），再 resolve ——
    "用哪个实现"在一个明确的位置决定，不藏在工具函数里。

    三种实现（本阶段起）：local = 裸磁盘；**jail = 磁盘 + 写围栏（本阶段新增）**；
    memory = 纯内存。jail 用装饰器把 LocalFS 包起来——策略叠在机制之上。
    """
    services = ServiceContainer()
    workspace = Path(args.workspace)
    if args.fs == "memory":
        services.register(FS_CAPABILITY, MemoryFS())
        label = "MemoryFS（文件只活在内存里）"
    elif args.fs == "jail":
        workspace.mkdir(parents=True, exist_ok=True)
        services.register(FS_CAPABILITY, WorkspaceJailFS(LocalFS(workspace)))
        label = f"WorkspaceJailFS(LocalFS)（改动必须落在 {args.workspace} 内；越界被拒）"
    else:
        workspace.mkdir(parents=True, exist_ok=True)  # 本地 provider 的工作区先建好
        services.register(FS_CAPABILITY, LocalFS(workspace))
        label = f"LocalFS（{args.workspace}）"
    fs = services.resolve(FS_CAPABILITY)  # ★ 显式解析点
    return fs, label


def build_subprocess_service(args: argparse.Namespace) -> tuple[object, str]:
    """★ 本阶段机制：按 --shell 选择并 resolve 一个 SubprocessService。

    `local` = LocalSubprocess（真实启动子进程）；
    `fake`  = ScriptedSubprocess（剧本回放，**一条命令都不真跑**——安全的演示模式，
              也是"工具只认协议"的最直白证据）。

    与 build_file_system 同一套纪律：register（单槽、重复报错）→ resolve（显式解析点）。
    离线剧本（--fake）会自动切到 fake——跑测试/演示时谁也不想真执行命令。
    """
    choice = args.shell
    if choice == "auto":
        choice = "fake" if args.fake else "local"
    services = ServiceContainer()
    if choice == "fake":
        # 剧本预置三条结果：正常 / 非零退出 / 超时，够演示三种渲染。
        services.register(
            SUBPROCESS_CAPABILITY,
            ScriptedSubprocess(
                [
                    CommandResult(0, "演示输出：命令正常结束。\n"),
                    CommandResult(1, "", "演示输出：命令以退出码 1 结束。\n"),
                    CommandResult(None, "演示输出：跑到一半被超时终止。\n", timed_out=True),
                ]
            ),
        )
        label = "ScriptedSubprocess（剧本回放，不真跑任何命令）"
    else:
        services.register(SUBPROCESS_CAPABILITY, LocalSubprocess())
        label = "LocalSubprocess（真实启动子进程；子进程环境已剔除凭证变量）"
    return services.resolve(SUBPROCESS_CAPABILITY), label


# =========================================================================
# 2) 前序机制的装配（沿用 _03）：section 装配 + 装配单展示
# =========================================================================


def build_assembler(*, with_scope: bool = True) -> tuple[PromptAssembler, PromptAssembler]:
    """构造装配器，返回 (基础装配器, 生效装配器)：基础四节 + chat 作用域追加一节。"""
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
    """打印一份装配单（来自日志），末尾做"可由日志重建"的校验。"""
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


# =========================================================================
# 3) provider / 审批（与基线 cli 一致的构造方式）
# =========================================================================


def build_provider(args: argparse.Namespace):
    """真实 API 优先；--fake 用离线剧本。

    离线剧本演示两件事：**shell 工具**（本阶段机制——用剧本 provider 时它不真跑
    命令，只回放三种结局）与**越界写**（`_05` 机制——fs=local 真的写出去、
    fs=jail 结构化拒绝）。（剧本是死板的：它不会"看"你的提问内容。）
    """
    if args.fake:
        script = [
            tool_call_reply("shell", {"command": "echo demo"}, call_id="call_sh"),
            text_reply("命令执行的结果见上方工具日志。（离线剧本）"),
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hello"}),
            text_reply("已把 hello 写入 notes/a.txt。（离线剧本）"),
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
    return sum(1 for event in ctx.session.events if event.type == "system/message")


# =========================================================================
# 4) 对话循环
# =========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chat.py", description="能力接缝对话：文件世界 + 命令执行，都是可替换的"
    )
    parser.add_argument("--session", "-s", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="文件 provider 的工作区目录")
    parser.add_argument(
        "--fs", choices=("local", "jail", "memory"), default="local",
        help="文件系统 provider：local=真实磁盘；jail=磁盘+写围栏（越界被拒）；memory=只写内存",
    )
    parser.add_argument(
        "--shell", choices=("auto", "local", "fake"), default="auto",
        help="命令执行 provider：local=真实进程；fake=剧本回放（不真跑）；auto=--fake 时用 fake，否则 local",
    )
    parser.add_argument("--shell-timeout", type=float, default=15.0, help="shell 工具的期限（秒）")
    parser.add_argument("--fake", action="store_true", help="离线剧本（不需要 API key）")
    parser.add_argument("--ask", default=None, help="一条问题跑一个 turn 后退出")
    parser.add_argument("--dump-prompt", action="store_true", help="打印装配单（来源 + 重建校验）")
    parser.add_argument("--search", action="store_true", help="额外启用 web_search 工具（需网络）")
    parser.add_argument("--no-scope", action="store_true", help="不叠加 chat 作用域，只用基础四节")
    parser.add_argument("--no-approve", action="store_true", help="写文件自动放行（跳过审批）")
    parser.add_argument("--deny", action="store_true", help="禁止一切需审批操作")
    args = parser.parse_args(argv)

    # ---- ★ 本阶段机制：显式选择 + resolve 两个 provider（fs 与 shell）----
    fs, fs_label = build_file_system(args)
    shell_service, shell_label = build_subprocess_service(args)
    registry = build_toolbox(
        fs,
        shell_service,
        workspace=args.workspace,
        include_search=args.search,
        shell_timeout=args.shell_timeout,
    )

    # ---- 前序机制：装配系统提示词 ----
    _, active = build_assembler(with_scope=not args.no_scope)

    # ---- 开一场会话：接缝工具 + 每 step 渲染 ----
    inner, mode = build_provider(args)
    ctx = open_context_harness(
        args.session,
        provider=inner,
        root=args.root,
        workspace=args.workspace,
        assembler=active,
        context_source=lambda: collect_runtime_context(Path(args.workspace)),
        approval=build_approval(args),
        tool_registry=registry,
    )
    harness = ctx.harness

    print("=" * 68)
    print(f"provider：{mode}")
    print(f"文件系统：{fs_label}    ← 接缝一（_04/_05）")
    print(f"命令执行：{shell_label}    ← 接缝二（本阶段机制）")
    print(f"会话：{harness.session.session_id}    已有 turn：{harness.session.last_turn_number}")
    print(f"工具：{[spec.name for spec in harness.tools]}")
    if ctx.report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {ctx.report.dropped_bytes} 字节）")
    print("查看：--dump-prompt / /prompt（装配单）；/fs、/shell（当前 provider）；/context；/exit")

    if args.dump_prompt:
        dump_latest(ctx, "开场装配单")

    if args.ask is not None:
        return _ask_once(ctx, args, fs, shell_service)

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
            print("  /prompt 装配单；/context 生效文本；/fs、/shell 当前 provider；/history；/exit 退出。")
            continue
        if text == "/fs":
            print(f"  当前文件系统：{fs_label}")
            print(f"  类型：{type(fs).__name__}")
            continue
        if text == "/shell":
            print(f"  当前命令执行：{shell_label}")
            print(f"  类型：{type(shell_service).__name__}；期限 {args.shell_timeout:g} 秒")
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
    if isinstance(fs, MemoryFS):
        print("（本次用的是 MemoryFS：写入的文件已随进程结束消失，磁盘上没有任何痕迹）")
    elif isinstance(fs, WorkspaceJailFS):
        print("（本次用的是 WorkspaceJailFS：所有改动都被限制在工作区内，越界已拒）")
    if isinstance(shell_service, ScriptedSubprocess):
        print("（本次命令执行用的是 ScriptedSubprocess：剧本回放，一条命令都没真跑）")
    print(f"下次继续：python chat.py --session {args.session} --dump-prompt")
    return 0


def _ask_once(ctx, args: argparse.Namespace, fs, shell_service) -> int:
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
    if isinstance(fs, MemoryFS):
        print("（MemoryFS：文件只活在内存里，磁盘无痕迹）")
    elif isinstance(fs, WorkspaceJailFS):
        print("（WorkspaceJailFS：改动被限制在工作区内；越界请求会被结构化拒绝）")
    if isinstance(shell_service, ScriptedSubprocess):
        print("（ScriptedSubprocess：命令走剧本回放，没有真执行）")
    print(f"日志：{JsonlStore(Path(args.root)).path(args.session)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
