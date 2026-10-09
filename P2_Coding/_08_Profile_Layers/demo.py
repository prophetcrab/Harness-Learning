"""_08_Profile_Layers 的入口脚本：把 mini harness 完整跑一遍（全离线）。

运行方式（在本阶段目录下）：

    python demo.py            # 全离线（FakeLLM / MemoryFS / 剧本命令）
    python demo.py --keep     # 保留 demo_run/（默认每次清空重建）

故事线 = 本阶段机制演示 + 前序机制回归：

    0a. [M6 _08 新增] 分层：base 树 → dev 补丁 → 用户补丁 → CLI 补丁，
        每层只动它关心的行，后写胜出；两层对照（dev vs prod）差异可枚举
    0b. 一行 patch 换 provider：同一份 dev 骨架，只把 llm 行的 name 换掉
    0c. fail loud：未知 id / 空 patch / 未知插件名 —— 配置错误启动即报错
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
    Patch,
    PatchRow,
    apply_patch,
    load_patch,
    load_profile,
    load_tree,
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
    # 0a) [本阶段新增] 配置分层：每层只动关心的行，后写胜出
    # =====================================================================
    print("=" * 68)
    print("0a) profiles 分层：base → profile 补丁 → 用户补丁 → CLI 补丁")
    base = load_tree(PROFILES / "base.yaml")
    print_tree(base, "第一层 base.yaml（底座：中性默认）")

    dev_patch = load_patch(PROFILES / "dev.yaml")
    dev_tree = apply_patch(base, dev_patch)
    print_tree(dev_tree, "第二层 + dev.yaml（把三行换成离线实现）")

    user_patch_path = DEMO_RUN / "user.patch.yaml"
    user_patch_path.write_text(
        "patch:\n  - id: toolbox\n    config:\n      workspace: demo_workspace/ws\n"
        "      shell_timeout: 5\n",
        encoding="utf-8",
    )
    user_tree = apply_patch(dev_tree, load_patch(user_patch_path))
    print_tree(user_tree, "第三层 + 用户补丁（只把 toolbox 的期限收到 5 秒）")

    cli_patch_path = DEMO_RUN / "cli.patch.yaml"
    cli_patch_path.write_text(
        "patch:\n  - id: subprocess\n    disabled: true\n", encoding="utf-8"
    )
    final_tree = apply_patch(user_tree, load_patch(cli_patch_path))
    print_tree(final_tree, "第四层 + CLI 补丁（禁用 subprocess 行——行还在，不激活）")

    print(f"   最终树：共 {len(final_tree.rows)} 行，激活 {len(final_tree.active_rows())} 行")
    assert final_tree.require("toolbox").config["shell_timeout"] == 5
    assert final_tree.require("subprocess").disabled is True
    assert final_tree.require("subprocess").name == "subprocess:scripted"  # 其余字段不受影响

    dev = load_profile("dev", profiles_dir=PROFILES)
    prod = load_profile("prod", profiles_dir=PROFILES)
    print("\n   dev vs prod（同 base，差异全部落在哪几行）：")
    changed = [
        row.id
        for row, other in zip(dev.rows, prod.rows, strict=True)
        if (row.name, row.config) != (other.name, other.config)
    ]
    for row_id in changed:
        left, right = dev.require(row_id), prod.require(row_id)
        print(f"     - {row_id:<10} {left.name:<20} →  {right.name}")
    assert dev.ids == prod.ids  # 骨架相同
    assert changed == ["llm", "fs", "subprocess"]

    # =====================================================================
    # 0b) 一行 patch 换 provider
    # =====================================================================
    print("\n" + "=" * 68)
    print("0b) 一行 patch 换 provider：同样的 dev 骨架，只换 llm 行")
    one_line = Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")])
    switched = apply_patch(dev, one_line)
    print(f"   dev.llm       = {dev.require('llm').name}")
    print(f"   switched.llm  = {switched.require('llm').name}")
    print(f"   其余行不变：{[r.name for r in switched.rows[1:]]}")
    assert switched.require("llm").name == "llm:deepseek"
    assert [r.name for r in switched.rows[1:]] == [r.name for r in dev.rows[1:]]
    print("   → 能力实现的选择是数据：改一行，无需碰其它任何代码")

    # =====================================================================
    # 0c) fail loud：配置错误启动即报错
    # =====================================================================
    print("\n" + "=" * 68)
    print("0c) 配置错误的 fail loud（不静默跳过）")
    try:
        apply_patch(dev, Patch(replacements=[PatchRow(id="nope", disabled=True)]))
        print("   !! patch 指向不存在的行: 竟然没报错")
    except ConfigError as exc:
        print(f"   patch 指向不存在的行：{exc}")

    try:  # 空 patch 行在"读文件"阶段就被拦下（更早、更省事）
        load_patch(_write_bad(DEMO_RUN / "empty.patch.yaml", "patch:\n  - id: toolbox\n"))
        print("   !! 空 patch 行: 竟然没报错")
    except ConfigError as exc:
        print(f"   空 patch 行：{exc}")

    ghost = apply_patch(dev, Patch(replacements=[PatchRow(id="llm", name="llm:ghost")]))
    try:
        build_plugins_from_tree(ghost, stage_root=HERE)
    except ConfigError as exc:
        print(f"   未知插件名：{str(exc)[:64]}……")

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
