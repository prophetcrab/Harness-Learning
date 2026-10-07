"""_01_Prompt_Sections 的新机制单元测试：section 注册表 + 装配器。

与同目录另两个文件的分工：
- `test_prompt_sections_assembly.py` / `_webui.py` 是 **M1–M3 的组装基线**（回归，22 用例）；
- 本文件只测试 **M4 `_01` 新增的机制**（section 注册表 / 作用域遮蔽 / 装配器），
  以及它与 `MiniHarness.open` 的接线。

覆盖：
1. 装配顺序 = 注册顺序
2. 基础层重名 fail loud
3. 作用域：同名遮蔽（保持原位）/ 新增追加 / 撤下后基础层恢复 / close 后不可写
4. 装配器：分隔符、parts() 反映遮蔽、assemble 幂等（快照稳定）
5. 接线：MiniHarness.open 接受 assembler，装配结果进 session/start 并可投影

运行：cd P2_Coding/_01_Prompt_Sections && python -m pytest -q
"""

from __future__ import annotations

import pytest

from harness.llm import FakeLLM, text_reply
from harness.mini import MiniHarness
from harness.session import JsonlStore
from prompt import (
    PromptAssembler,
    Section,
    SectionRegistry,
    default_assembler,
    default_sections,
)


def _reg(*sections: Section) -> SectionRegistry:
    """便捷：把若干 section 注册成一个基础注册表。"""
    registry = SectionRegistry()
    for section in sections:
        registry.register(section)
    return registry


# =========================================================================
# 1) 基础层：顺序与重名
# =========================================================================


def test_sections_follow_registration_order():
    """装配顺序 = 注册顺序。"""
    registry = _reg(
        Section("a", "第一节"),
        Section("b", "第二节"),
        Section("c", "第三节"),
    )
    assert registry.names == ["a", "b", "c"]
    assert PromptAssembler(registry).assemble() == "第一节\n\n第二节\n\n第三节"


def test_duplicate_registration_is_loud():
    """基础层重复注册同名 → 直接报错（fail loud，不静默覆盖）。"""
    registry = _reg(Section("role", "原始"))
    with pytest.raises(ValueError, match="重名"):
        registry.register(Section("role", "另一个"))


def test_unregister_removes_section():
    """unregister 能移除基础层的一节；不存在则报错。"""
    registry = _reg(Section("a", "A"), Section("b", "B"))
    registry.unregister("a")
    assert registry.names == ["b"]
    with pytest.raises(KeyError):
        registry.unregister("a")


def test_empty_name_is_rejected():
    """空名字无法注册/遮蔽，构造 Section 时就应报错。"""
    with pytest.raises(ValueError, match="不能为空"):
        Section("", "内容")


# =========================================================================
# 2) 作用域：遮蔽 / 追加 / 恢复
# =========================================================================


def test_scope_shadows_in_place_and_appends_new():
    """作用域：同名遮蔽**保持原位**，未同名的新增**追加到末尾**。"""
    registry = _reg(Section("a", "A"), Section("b", "B"))
    scope = registry.scoped("session")

    scope.register(Section("b", "B-覆盖"))   # 遮蔽已有的 b（应保持在第 2 位）
    scope.register(Section("c", "C-新增"))   # 新增 c（应追加到末尾）

    assert scope.names == ["a", "b", "c"]
    assert PromptAssembler(scope).assemble() == "A\n\nB-覆盖\n\nC-新增"
    # 基础层未被改动 —— 遮蔽不是删除。
    assert registry.names == ["a", "b"]


def test_scope_drop_restores_base():
    """drop 撤销本层遮蔽后，基础层同名 section 重新可见。"""
    registry = _reg(Section("a", "A"))
    scope = registry.scoped("session")
    scope.register(Section("a", "A-覆盖"))
    assert PromptAssembler(scope).assemble() == "A-覆盖"

    scope.drop("a")
    assert PromptAssembler(scope).assemble() == "A"   # 基础层恢复


def test_scope_close_restores_base_and_freezes():
    """close() 撤下整个作用域（基础层完全恢复），且之后不再接受写入。"""
    registry = _reg(Section("a", "A"))
    scope = registry.scoped("session")
    scope.register(Section("a", "A-覆盖"))
    scope.register(Section("extra", "额外"))

    scope.close()
    assert PromptAssembler(scope).assemble() == "A"       # 全部回卷
    with pytest.raises(RuntimeError, match="已关闭"):
        scope.register(Section("x", "x"))


def test_scope_records_source():
    """作用域注册的 section 会带上该作用域作为来源（便于追溯）。"""
    registry = _reg(Section("a", "A"))
    scope = registry.scoped("my-scope")
    scope.register(Section("a", "A-覆盖"))
    assert scope.sections()[0].source == "my-scope"


# =========================================================================
# 3) 装配器
# =========================================================================


def test_assembler_custom_separator_and_parts():
    """分隔符可配；parts() 反映当前（含遮蔽的）装配顺序。"""
    registry = _reg(Section("a", "A"), Section("b", "B"))
    scope = registry.scoped("s")
    scope.register(Section("b", "BB"))

    assert PromptAssembler(scope, separator=" | ").assemble() == "A | BB"
    assert [s.name for s in PromptAssembler(scope).parts()] == ["a", "b"]


def test_assemble_is_stable_snapshot():
    """同一份 section 装配两次结果一致（快照稳定）。"""
    assembler = default_assembler()
    assert assembler.assemble() == assembler.assemble()
    assert str(assembler) == assembler.assemble()


def test_default_sections_are_composable():
    """内置默认：拆成多节，可按名字单独取用（证明提示词是拼出来的）。"""
    names = [s.name for s in default_sections()]
    assert names == ["role", "tools", "style"]
    assembled = default_assembler().assemble()
    for sentence in ("你是一名严谨的中文助手", "calculate", "最终回答用中文"):
        assert sentence in assembled


# =========================================================================
# 4) 装配产物接入 MiniHarness（harness/ 基线本阶段未改）
#
# 本阶段的机制是"顶层模块 prompt/"：先把 section 装配成一段文本，再把这段文本交给
# `harness.mini.MiniHarness.open(system_prompt=...)`。所以这里的接线就是"先装配、再传入"。
# =========================================================================


def test_assembled_prompt_goes_into_log(tmp_path):
    """装配产物写进 session/start，并可投影成 system 消息。"""
    assembler = PromptAssembler(_reg(Section("role", "你是测试助手。"), Section("style", "简短。")))
    harness, _ = MiniHarness.open(
        "s1",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        system_prompt=assembler.assemble(),   # ← 装配在此，harness 只收成品文本
    )

    # session/start 事件里存的就是装配后的文本
    events, _ = JsonlStore(tmp_path / "sessions").load("s1")
    start = next(e for e in events if e.type == "session/start")
    assert start.data["system_prompt"] == "你是测试助手。\n\n简短。"
    # 投影出 system 消息
    assert harness.messages[0].role == "system"
    assert harness.messages[0].content == "你是测试助手。\n\n简短。"


def test_scope_override_flows_into_log(tmp_path):
    """带作用域遮蔽的装配器，其最终文本被写进日志。"""
    registry = _reg(Section("role", "基础角色。"))
    scope = registry.scoped("custom")
    scope.register(Section("role", "覆盖角色。"))

    harness, _ = MiniHarness.open(
        "s2",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        system_prompt=PromptAssembler(scope).assemble(),
    )
    assert harness.messages[0].content == "覆盖角色。"


def test_open_keeps_str_api(tmp_path):
    """M1–M3 基线接口不变：`system_prompt` 仍收一段成品字符串。"""
    harness, _ = MiniHarness.open(
        "s3",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        system_prompt="系统提示",
    )
    assert harness.messages[0].content == "系统提示"


def test_default_assembler_matches_baseline_semantics(tmp_path):
    """内置默认装配器与 harness 基线默认提示词语义一致（三句都在）。

    注意：默认装配器用空行分隔各节，而基线 `DEFAULT_SYSTEM_PROMPT` 是无分隔拼接，
    因此二者**字节不等**、语义等价——这正是"从字符串升级成 section"带来的可见变化。
    """
    from harness.mini import DEFAULT_SYSTEM_PROMPT

    assembled = default_assembler().assemble()
    for sentence in ("你是一名严谨的中文助手", "calculate", "最终回答用中文"):
        assert sentence in assembled
        assert sentence in DEFAULT_SYSTEM_PROMPT

    # 装配产物（空行分段）交给 MiniHarness.open，成为 session/start 里的提示词
    harness, _ = MiniHarness.open(
        "s4",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        system_prompt=assembled,
    )
    assert harness.messages[0].content == assembled
