"""_09_Dump_Config 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线（FakeLLM / MemoryFS / 剧本命令）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + 前序机制回归：

    0a. [M6 _09 新增] dump-config 的来源追踪：每一行/每个字段标出"来自哪一层"
        （base.yaml / dev.yaml / user.patch.yaml / CLI#N …）+ 按层汇总
    0b. 错误定位：配错启动即报错，且指出"哪一层、第几条"；未知名字/键给拼写建议
    0c. 前序机制回归：分层（_08）与一行换 provider 仍成立，来源随层更新
    1. 主故事：boot dev profile（FakeLLM + MemoryFS + 剧本命令）跑多轮对话；
       结束后整体卸载看回卷
    2. 打印会话日志；3. 模拟退出；4. resume；5. 同构断言；6. 分叉
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    ConfigError,
    ConfigRow,
    Patch,
    PatchRow,
    apply_patch,
    load_patch,
    load_profile,
    render_tree,
    source_summary,
)
from context import (
    collect_runtime_context,
    effective_system_prompt,
    latest_prompt_trace,
    open_context_harness,
    project,
)
from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.runner import fork_session, load_messages
from harness.session import JsonlStore
from harness.tools import ScriptedApprover
from harness.tools.approval import ApprovalDecision
from prompt import rebuild_text
from providers import (
    LLM_CAPABILITY,
    TOOLBOX_CAPABILITY,
    boot_tree,
    build_plugins_from_tree,
)

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
DEMO_RUN = HERE / "demo_run"
SESSIONS = DEMO_RUN / "sessions"
PROFILES = HERE / "profiles"

TZ = timezone(timedelta(hours=8))
STORY_MOMENT = datetime(2026, 10, 8, 9, 0, 0, tzinfo=TZ)


def plain(messages):
    """把消息压成可比较的元组序列（同构断言用）。"""
    return [
        (m.role, m.content, tuple((c.id, c.name) for c in m.tool_calls), m.tool_call_id)
        for m in messages
    ]


def _write_bad(path: Path, text: str) -> Path:
    """往临时文件写一段（demo 里构造"坏配置"用）。"""
    path.write_text(text, encoding="utf-8")
    return path


def print_tree(tree, title: str) -> None:
    """打印一棵树的轮廓（id + name + 禁用标记）。"""
    print(f"   {title}")
    for row in tree.rows:
        mark = "（禁用）" if row.disabled else ""
        print(f"     - {row.id:<10} {row.name}{mark}")


def main() -> None:
    keep = "--keep" in sys.argv
    if DEMO_RUN.exists() and not keep:
        shutil.rmtree(DEMO_RUN)
    DEMO_RUN.mkdir(parents=True, exist_ok=True)

    # =====================================================================
    # 0a) [本阶段新增] 来源追踪：每一行/每个字段都标出"来自哪一层"
    # =====================================================================
    print("=" * 68)
    print("0a) dump-config 的来源追踪：值的来历一眼可读")
    user_patch_path = DEMO_RUN / "user.patch.yaml"
    user_patch_path.write_text(
        "patch:\n  - id: toolbox\n    config:\n      workspace: demo_workspace/ws\n"
        "      shell_timeout: 5\n",
        encoding="utf-8",
    )
    cli_patch_path = DEMO_RUN / "cli.patch.yaml"
    cli_patch_path.write_text("patch:\n  - id: llm\n    name: llm:deepseek\n", encoding="utf-8")

    tree = load_profile(
        "dev",
        profiles_dir=PROFILES,
        cli_patches=[user_patch_path, cli_patch_path],
    )
    print()
    print(render_tree(tree, title="dev + 2 层 CLI 补丁（每项末尾 ← 是它的来源层）"))
    print("\n   读法：第 4 行的 shell_timeout 来自 user.patch.yaml；第 1 行的 name 来自")
    print("   CLI#2——它们都覆盖了 dev.yaml 的值；没被碰过的字段仍标着 dev.yaml / base.yaml。")

    summary = source_summary(tree)
    print("\n   按层汇总（每层贡献了哪些字段）：")
    for layer, items in summary.items():
        print(f"     {layer}: {items}")
    assert tree.source_of("llm", "name") == "CLI#2 cli.patch.yaml"
    assert tree.source_of("toolbox", "config") == "CLI#1 user.patch.yaml"
    assert tree.source_of("fs", "name") == "dev.yaml"

    # =====================================================================
    # 0b) 错误定位：配错启动即报错，且指出"哪一层、第几条、是不是拼错了"
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) 配置错误的定位（层名 + 条目序号 + 拼写建议）")
    cases: list[tuple[str, callable]] = [
        (
            "patch 指向不存在的行 id",
            lambda: apply_patch(
                tree,
                Patch(replacements=[PatchRow(id="toolbx", disabled=True)], label="my.patch.yaml"),
            ),
        ),
        (
            "insert 撞已有 id",
            lambda: apply_patch(
                tree,
                Patch(inserts=[ConfigRow(id="llm", name="llm:fake")], label="my.patch.yaml"),
            ),
        ),
        (
            "未知插件名（构造阶段拦）",
            lambda: build_plugins_from_tree(
                apply_patch(
                    tree,
                    Patch(replacements=[PatchRow(id="llm", name="llm:fake2")], label="my.patch.yaml"),
                ),
                stage_root=HERE,
            ),
        ),
        (
            "未知参数键（拼写建议）",
            lambda: build_plugins_from_tree(
                apply_patch(
                    tree,
                    Patch(
                        replacements=[
                            PatchRow(id="toolbox", config={"shell_timeou": 3}),
                        ],
                        label="my.patch.yaml",
                    ),
                ),
                stage_root=HERE,
            ),
        ),
        (
            "参数类型错（带行定位）",
            lambda: build_plugins_from_tree(
                apply_patch(
                    tree,
                    Patch(
                        replacements=[PatchRow(id="toolbox", config={"shell_timeout": "很快"})],
                        label="my.patch.yaml",
                    ),
                ),
                stage_root=HERE,
            ),
        ),
        (
            "YAML 语法错（文件级）",
            lambda: load_patch(_write_bad(DEMO_RUN / "bad.yaml", "patch: [unclosed\n")),
        ),
    ]
    for label, action in cases:
        try:
            action()
            print(f"   !! {label}: 竟然没报错")
        except ConfigError as exc:
            print(f"   [{label}]")
            print(f"     {exc}")

    # 对照：合法配置照常跑通（错误定位不是把正常路径也拦了）
    good = apply_patch(
        tree,
        Patch(replacements=[PatchRow(id="llm", name="llm:fake")], label="ok.patch.yaml"),
    )
    plugins = build_plugins_from_tree(good, stage_root=HERE)
    print(f"\n   对照：合法配置照常构造 → {[p.name for p in plugins]}")

    # =====================================================================
    # 0c) 前序机制回归：分层与一行换 provider（_08 语义仍成立）
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 前序机制回归：分层（_08）仍成立")
    dev = load_profile("dev", profiles_dir=PROFILES)
    prod = load_profile("prod", profiles_dir=PROFILES)
    changed = [
        row.id
        for row, other in zip(dev.rows, prod.rows, strict=True)
        if (row.name, row.config) != (other.name, other.config)
    ]
    print(f"   dev vs prod 差异行：{changed}（骨架相同：{dev.ids == prod.ids}）")
    assert changed == ["llm", "fs", "subprocess"]

    one_line = Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")], label="one-line")
    switched = apply_patch(dev, one_line)
    assert switched.require("llm").name == "llm:deepseek"
    print(f"   一行 patch 换 provider：{dev.require('llm').name} → {switched.require('llm').name}")
    print(f"   且来源随之更新：llm.name ← {switched.source_of('llm', 'name')}")

    # =====================================================================
    # 1) 主故事：boot dev profile 跑多轮对话
    # =====================================================================
    print("\n" + "=" * 68)
    print("1) 主故事：boot dev profile（FakeLLM + MemoryFS + 剧本命令）")
    story_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    llm = story_ctx.require(LLM_CAPABILITY)
    registry = story_ctx.require(TOOLBOX_CAPABILITY)
    print(f"   插件台账：{story_ctx.effect_names}")
    print(f"   工具面：{registry.names}")

    workspace = DEMO_RUN / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    approval = ScriptedApprover(
        [
            ApprovalDecision(True, "demo：批准执行命令（剧本，不真跑）"),
            ApprovalDecision(True, "demo：批准写入"),
        ]
    )
    ctx = open_context_harness(
        "demo",
        provider=llm,
        root=SESSIONS,
        workspace=workspace,
        approval=approval,
        context_source=lambda: collect_runtime_context(
            workspace, now=lambda: STORY_MOMENT, platform_name="DemoOS"
        ),
        tool_registry=registry,
    )
    for question in [
        "帮我算 1234*56.78",
        "用 shell 看一眼目录",
        "把要点记到 notes/todo.txt",
    ]:
        result = ctx.harness.send(question)
        print(f"   你：{question}")
        print(f"   助手：{result.final_text}")

    online_history = plain(ctx.harness.history)
    approval.assert_all_consumed()
    print("   ✓ 命令与写入两次审批都用在了正确的位置（dev：全离线，零真实副作用）")
    assert list(workspace.iterdir()) == []  # dev 的 fs 是 MemoryFS：磁盘无痕迹
    print("   ✓ MemoryFS 生效：工作区目录里没有任何文件（写只进内存）")

    print(f"   整体卸载前槽位：{story_ctx.slots}")
    for name in reversed(story_ctx.effect_names):
        story_ctx.unload(name)
    print(f"   整体卸载后槽位：{story_ctx.slots}")
    assert story_ctx.slots == []

    # =====================================================================
    # 2) 打印会话日志
    # =====================================================================
    print("\n" + "=" * 68)
    print("2) 会话日志（append-only 事件序列，逐条落盘）")
    for event in ctx.session.events:
        mark = "  <-trace" if event.data.get("prompt_trace") else ""
        print(f"   #{event.seq:>2} {event.type}{mark}")
    print(f"   日志文件：{JsonlStore(SESSIONS).path('demo')}")

    # =====================================================================
    # 3) 模拟"退出"
    # =====================================================================
    print("\n" + "=" * 68)
    print("3) 退出：丢弃进程内的 harness 对象（历史只留在磁盘日志里）")
    del ctx
    print("   进程内已无任何会话状态。")

    # =====================================================================
    # 4) resume（重新 boot 一份配置）
    # =====================================================================
    print("\n" + "=" * 68)
    print("4) resume：重新加载配置、重新打开会话（对旧 id 是恢复 —— 同一段代码）")
    resume_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=HERE)
    resume_script = FakeLLM(
        [
            tool_call_reply("list_files", {"path": "."}),
            text_reply("内存里记着笔记（MemoryFS 不跨进程）。"),
        ]
    )
    resumed_harness_ctx = open_context_harness(
        "demo",
        provider=resume_script,
        root=SESSIONS,
        workspace=workspace,
        approval=ScriptedApprover([]),
        context_source=lambda: collect_runtime_context(
            workspace, now=lambda: STORY_MOMENT, platform_name="DemoOS"
        ),
        tool_registry=resume_ctx.require(TOOLBOX_CAPABILITY),
    )
    resumed = resumed_harness_ctx.harness
    print(f"   resume 时的历史角色序列：{[m.role for m in resumed.messages]}")
    print(f"   接续 turn 编号：{resumed.session.last_turn_number}")
    r4 = resumed.send("工作区里有哪些文件？")
    print(f"   你：工作区里有哪些文件？\n   助手：{r4.final_text}")
    resume_script.assert_all_consumed()

    # =====================================================================
    # 5) 同构断言
    # =====================================================================
    print("\n" + "=" * 68)
    print("5) 同构断言：重放日志得到的历史 == 在线产生的历史")
    replayed = plain(load_messages("demo", JsonlStore(SESSIONS)))
    assert replayed == plain(resumed.history), "重放历史与在线历史不一致！"
    assert replayed[: len(online_history)] == online_history, "前期在线历史与重放前缀不一致！"
    extended = plain(project(resumed_harness_ctx.session.events))
    assert extended == plain(resumed.history), "扩展投影与在线历史不一致！"
    print("   ✓ 一致（消息历史完全由日志投影而来，且前期快照是重放前缀）")
    latest = latest_prompt_trace(resumed_harness_ctx.session.events)
    assert rebuild_text(latest) == effective_system_prompt(resumed_harness_ctx.session.events)
    print("   ✓ 当前生效提示词 == rebuild(日志装配单)（提示词机制在配置装配下照常工作）")

    # =====================================================================
    # 6) 分叉
    # =====================================================================
    print("\n" + "=" * 68)
    print("6) 分叉：从第 6 条事件处派生一个新会话")
    store = JsonlStore(SESSIONS)
    forked = fork_session(store, "demo", "demo-fork", upto_seq=6)
    print(f"   demo-fork 事件：{[e.type for e in forked.events]}")
    print(f"   原会话 demo 仍是 {len(resumed.session.events)} 条（未被改动）")
    print(f"   现有会话：{store.list_sessions()}")

    print("\n全部步骤完成。可再试：python chat.py --dump-config / python chat.py --profile prod")


if __name__ == "__main__":
    main()
