"""chat.py —— 配置驱动的对话入口：装配不再写代码，改一行配置就换实现。

这是本阶段（`_08_Profile_Layers`）的**交互式入口**，回答一个问题：
"把'装什么能力'从代码搬进 profiles 配置之后，换实现有多容易？"

它做的事：
1. **配置分层**（★ 本阶段机制）：`load_profile(name)` 按
   base → profile → 用户补丁 → CLI `--patch` 的顺序叠出**最终配置树**；
   `--dump-config` 可以把这棵树打印出来对照；
2. **激活**（`_07` 机制）：`boot_tree(tree)` 把树变成插件 → 装进 `kernel.Context`
   ——注册即 effect，退出时整体卸载回卷；
3. 其余照旧：section 装配 + 每 step 渲染 + 装配单（`_01`–`_03` 的机制）。

用法（在 _08_Profile_Layers 目录下）：

    python chat.py                              # dev profile（离线：FakeLLM + MemoryFS）
    python chat.py --profile prod               # prod profile（DeepSeek + 真实磁盘/进程）
    python chat.py --dump-config                # 打印最终配置树（每行的来源层）
    python chat.py --patch my.yaml              # 叠加一层 CLI 补丁（可多次，后写胜出）
    python chat.py --dump-prompt --ask "帮我算 2+3"

会话内命令：/config 当前配置树；/ctx 插件台账与槽位；/prompt 装配单；/unload 回卷演示；
/exit 退出（自动整体卸载）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 本阶段新增的机制（顶层模块）
from config import ConfigError, ConfigTree, load_profile, render_tree

# 前序阶段的机制（顶层模块）
from context import (
    collect_runtime_context,
    effective_system_prompt,
    open_context_harness,
)

# 基线的会话/审批（未改动）
from harness.session import JsonlStore
from harness.tools import AutoApprove, PromptApprover
from harness.tools.approval import AutoDeny
from kernel import Context
from prompt import PromptAssembler, Section, default_registry, rebuild_text
from providers import (
    LLM_CAPABILITY,
    TOOLBOX_CAPABILITY,
    boot_tree,
)

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]  # _09_Dump_Config -> 项目根（读取 .env）
DEFAULT_ROOT = HERE / "sessions"
PROFILES_DIR = HERE / "profiles"
# 默认工作区放深一层：local 模式下"越界写"的落点（../）会落在 demo_workspace/ 里
# （已被 .gitignore 排除），既不污染仓库，又能直观看到"写到工作区外面了"。
DEFAULT_WORKSPACE = HERE / "demo_workspace" / "ws"


# =========================================================================
# 1) ★ 本阶段机制：加载配置树并激活 ★
# =========================================================================


def load_and_boot(profile: str, cli_patches: list[str]) -> tuple[Context, ConfigTree]:
    """加载 profile 的最终配置树，并激活成 ctx。

    配置错误（未知行 id、未知插件名、非法参数）在激活前 fail loud；
    激活失败（如槽位冲突）由 kernel 回卷已装部分——ctx 不残留半装状态。
    """
    tree = load_profile(profile, profiles_dir=PROFILES_DIR, cli_patches=cli_patches)
    context = boot_tree(tree, stage_root=HERE)
    return context, tree


def show_tree(tree: ConfigTree, title: str) -> None:
    """打印配置树——★ `_09` 机制：每一项带**来源层**（render_tree 渲染器）。"""
    print()
    print(render_tree(tree, title=title))


def resolve_workspace(tree: ConfigTree) -> Path:
    """从工具箱行的配置解析出会话工作区（相对路径按阶段目录解析）。"""
    row = tree.get("toolbox")
    raw = (row.config.get("workspace") if row else None) or str(DEFAULT_WORKSPACE)
    path = Path(str(raw))
    return path if path.is_absolute() else (HERE / path)


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
# 3) 审批（与基线 cli 一致的构造方式）
# =========================================================================


def build_approval(args: argparse.Namespace):
    if args.no_approve:
        return AutoApprove()
    if args.deny:
        return AutoDeny("--deny：本会话禁止一切需审批操作")
    return PromptApprover()


def _update_count(ctx) -> int:
    return sum(1 for event in ctx.session.events if event.type == "system/message")


def _unload_all(plug_ctx: Context) -> None:
    """整体卸载：逆序卸下每个插件 effect，打印回卷前后对照。"""
    print(f"\n退出前 ctx 槽位：{plug_ctx.slots}")
    for name in reversed(plug_ctx.effect_names):
        plug_ctx.unload(name)
    print(f"整体卸载后 ctx 槽位：{plug_ctx.slots}（注册物已自动回卷）")
    print("（这就是'注册即 effect'：每个插件装过的东西，卸载时按逆序自动撤销）")


# =========================================================================
# 4) 对话循环
# =========================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chat.py", description="配置驱动的对话入口：一行 patch 换 provider"
    )
    parser.add_argument("--profile", default="dev", help="profile 名（profiles/<name>.yaml）")
    parser.add_argument(
        "--patch", action="append", default=[], metavar="FILE",
        help="CLI 补丁层（可多次；后写的覆盖先写的，最后覆盖 profile 层）",
    )
    parser.add_argument("--dump-config", action="store_true", help="打印最终配置树后退出")
    parser.add_argument("--session", "-s", default="default", help="会话 id（同名即恢复继续）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="会话日志根目录")
    parser.add_argument("--ask", default=None, help="一条问题跑一个 turn 后退出")
    parser.add_argument("--dump-prompt", action="store_true", help="打印装配单（来源 + 重建校验）")
    parser.add_argument("--no-scope", action="store_true", help="不叠加 chat 作用域，只用基础四节")
    parser.add_argument("--no-approve", action="store_true", help="写文件/执行命令自动放行")
    parser.add_argument("--deny", action="store_true", help="禁止一切需审批操作")
    args = parser.parse_args(argv)

    # ---- ★ 本阶段机制：分层加载 + 激活 ----
    try:
        tree = load_profile(args.profile, profiles_dir=PROFILES_DIR, cli_patches=args.patch)
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        return 2

    if args.dump_config:
        show_tree(tree, f"profile = {args.profile}（最终配置树）")
        return 0

    try:
        plug_ctx = boot_tree(tree, stage_root=HERE)
    except (ConfigError, KeyError, ValueError) as exc:
        print(f"[激活失败] {exc}")
        return 2

    workspace = resolve_workspace(tree)
    llm = plug_ctx.require(LLM_CAPABILITY)
    registry = plug_ctx.require(TOOLBOX_CAPABILITY)

    # ---- 前序机制：装配系统提示词 ----
    _, active = build_assembler(with_scope=not args.no_scope)

    # ---- 开一场会话 ----
    ctx = open_context_harness(
        args.session,
        provider=llm,
        root=args.root,
        workspace=workspace,
        assembler=active,
        context_source=lambda: collect_runtime_context(workspace),
        approval=build_approval(args),
        tool_registry=registry,
    )
    harness = ctx.harness

    print("=" * 68)
    print(f"profile：{args.profile}" + (f"（+{len(args.patch)} 层 CLI 补丁）" if args.patch else ""))
    for row in tree.active_rows():
        print(f"  {row.id:<10} = {row.name}")
    print(f"插件台账：{plug_ctx.effect_names}")
    print(f"会话：{harness.session.session_id}    已有 turn：{harness.session.last_turn_number}")
    print(f"工具：{[spec.name for spec in harness.tools]}")
    if ctx.report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {ctx.report.dropped_bytes} 字节）")
    print("查看：/config 配置树；/ctx 插件台账；/prompt 装配单；/unload 回卷演示；/exit")

    if args.dump_prompt:
        dump_latest(ctx, "开场装配单")

    if args.ask is not None:
        return _ask_once(ctx, args, plug_ctx, registry)

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
            print("  /config 配置树；/ctx 插件台账；/prompt 装配单；/context 生效文本；"
                  "/unload 回卷演示；/history；/exit 退出。")
            continue
        if text == "/config":
            show_tree(tree, f"profile = {args.profile}（最终配置树）")
            continue
        if text == "/ctx":
            print(f"  插件台账（装载顺序）：{plug_ctx.effect_names}")
            print(f"  槽位：{plug_ctx.slots}")
            continue
        if text == "/unload":
            _demo_unload(plug_ctx, registry)
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
        except AssertionError:  # FakeLLM 剧本用完（dev profile 常见）
            if args.profile == "dev":
                print("（dev profile 的 FakeLLM 剧本已用完；真实对话请用 --profile prod）")
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
    _unload_all(plug_ctx)
    print(f"下次继续：python chat.py --profile {args.profile} --session {args.session}")
    return 0


def _demo_unload(plug_ctx: Context, registry) -> None:
    """/unload：卸载 toolbox 插件，现场看工具面槽位回卷（再装回去）。"""
    from kernel import load_plugins
    from providers import make_toolbox_plugin

    print(f"  卸载前槽位：{plug_ctx.slots}")
    plug_ctx.unload("plugin:toolbox")
    print(f"  卸载 toolbox 后：{plug_ctx.slots}  ← 工具面被撤销了")
    print("  （但 llm/fs/subprocess 槽位还在——只有被卸的那份动了）")
    load_plugins(plug_ctx, [make_toolbox_plugin()])  # 重装（effect 名已注销）
    print(f"  重新装载后：{plug_ctx.slots}  ← 又回来了（新装的工具面与原 registry 是两份）")
    print(f"  （会话仍用着原工具面对象：{len(registry.names)} 个工具，不受影响）")


def _ask_once(ctx, args: argparse.Namespace, plug_ctx: Context, registry) -> int:
    """--ask：一条问题跑一个 turn（结束后同样走整体卸载演示）。"""
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
    _unload_all(plug_ctx)  # 同样演示整体卸载回卷
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
