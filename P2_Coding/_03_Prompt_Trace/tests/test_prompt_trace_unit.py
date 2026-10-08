"""_03_Prompt_Trace 的单元测试：装配单捕获 / 序列化 / 重建 / 篡改检测。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（prompt/trace.py），以及它与装配器/插值器的契约。

覆盖：
1. 捕获：装配顺序、每节 name/source/title/模板/渲染文本/引用变量；文本 == assemble()
2. 重建：rebuild_text(装配单) == 记录文本（对象与字典两种入口）
3. 序列化往返：to_dict → JSON → from_dict → 重建一致
4. 检验有牙齿：改记录文本/改模板/缺变量 → 重建对不上或 fail loud
5. 作用域遮蔽与自定义分隔符被如实记录

运行：cd P2_Coding/_03_Prompt_Trace && python -m pytest -q
"""

from __future__ import annotations

import json

import pytest

from prompt import (
    PromptAssembler,
    Section,
    SectionRegistry,
    capture_trace,
    rebuild_text,
)
from prompt.trace import PromptTrace

VARS = {"cwd": "D:/ws", "platform": "DemoOS", "time": "2026-10-08T09:00:00+08:00"}


def _reg(*sections: Section) -> SectionRegistry:
    registry = SectionRegistry()
    for section in sections:
        registry.register(section)
    return registry


# =========================================================================
# 1) 捕获：配料表如实记录
# =========================================================================


def test_capture_records_sections_in_order():
    """装配单按装配顺序记录每节：name / source / title / 模板 / 渲染文本 / 引用变量。"""
    registry = _reg(
        Section("role", "你是助手。", title="角色"),
        Section("env", "目录 {{cwd}}，时间 {{time}}。", title="环境"),
    )
    trace = capture_trace(PromptAssembler(registry), {"cwd": "D:/w", "time": "T1"})

    assert [s.name for s in trace.sections] == ["role", "env"]
    assert [s.source for s in trace.sections] == ["builtin", "builtin"]
    assert trace.sections[0].title == "角色"
    assert trace.sections[0].template == "你是助手。"
    assert trace.sections[0].referenced == ()
    assert trace.sections[1].template == "目录 {{cwd}}，时间 {{time}}。"
    assert trace.sections[1].rendered == "目录 D:/w，时间 T1。"
    assert trace.sections[1].referenced == ("cwd", "time")


def test_capture_text_matches_assemble():
    """装配单的 text 与 assembler.assemble(variables) 逐字节一致（同一套渲染）。"""
    registry = _reg(
        Section("a", "静态。"),
        Section("env", "cwd={{cwd}}"),
    )
    assembler = PromptAssembler(registry)
    trace = capture_trace(assembler, {"cwd": "D:/w"})
    assert trace.text == assembler.assemble({"cwd": "D:/w"})
    assert trace.separator == assembler.separator


def test_capture_records_variables_used():
    """装配单记录本次渲染的全部变量取值。"""
    registry = _reg(Section("env", "{{cwd}}/{{time}}"))
    trace = capture_trace(PromptAssembler(registry), {"cwd": "w", "time": "t"})
    assert trace.variables == {"cwd": "w", "time": "t"}


def test_capture_unknown_variable_is_loud():
    """引用未提供的变量：与 assemble 一致，fail loud，不产生半份记录。"""
    registry = _reg(Section("env", "{{ghost}}"))
    with pytest.raises(ValueError, match="未知变量：ghost"):
        capture_trace(PromptAssembler(registry), {})


def test_capture_shadowed_source_recorded():
    """作用域遮蔽后：装配单记录的是**遮蔽版**（保持原位），来源被标为作用域名。"""
    registry = _reg(Section("role", "基础角色。"), Section("style", "简洁。"))
    scope = registry.scoped("custom")
    scope.register(Section("role", "覆盖角色。"))
    trace = capture_trace(PromptAssembler(scope), {})

    assert [s.name for s in trace.sections] == ["role", "style"]  # 原位遮蔽，顺序不变
    assert trace.sections[0].source == "custom"
    assert trace.sections[0].rendered == "覆盖角色。"


# =========================================================================
# 2) 重建：记录 → 文本
# =========================================================================


def test_rebuild_reproduces_text():
    """rebuild_text(装配单) == 记录文本——"由记录重建"的主断言。"""
    registry = _reg(
        Section("role", "你是助手。"),
        Section("env", "目录 {{cwd}}。"),
        Section("style", "简洁。"),
    )
    trace = capture_trace(PromptAssembler(registry), {"cwd": "D:/w"})
    assert rebuild_text(trace) == trace.text


def test_rebuild_from_dict_entry():
    """rebuild_text 也接受从日志读回的普通字典（from_dict 路径）。"""
    registry = _reg(Section("env", "{{cwd}}"))
    trace = capture_trace(PromptAssembler(registry), {"cwd": "D:/w"})
    assert rebuild_text(trace.to_dict()) == trace.text


def test_rebuild_with_custom_separator():
    """自定义分隔符被如实记录，重建时用同一个分隔符拼接。"""
    registry = _reg(Section("a", "A"), Section("b", "B"))
    trace = capture_trace(PromptAssembler(registry, separator=" | "), {})
    assert trace.separator == " | "
    assert trace.text == "A | B"
    assert rebuild_text(trace) == "A | B"


# =========================================================================
# 3) 序列化往返（模拟进日志 → 从磁盘读回）
# =========================================================================


def test_serialization_round_trip():
    """to_dict → JSON 编码/解码 → from_dict：字段无损、重建一致。"""
    registry = _reg(
        Section("role", "你是助手。", title="角色"),
        Section("env", "目录 {{cwd}}；平台 {{platform}}。", title="环境"),
    )
    scope = registry.scoped("chat")
    scope.register(Section("conversation", "多轮对话说明。"))
    trace = capture_trace(PromptAssembler(scope), {"cwd": "D:/w", "platform": "DemoOS"})

    wire = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
    restored = PromptTrace.from_dict(wire)
    assert restored == trace  # frozen dataclass：结构逐字段相等
    assert rebuild_text(restored) == trace.text


# =========================================================================
# 4) 检验有牙齿：记录被动过 / 缺数据，都不是静默通过
# =========================================================================


def test_tampered_text_is_detected():
    """改掉记录里的最终文本 → 重建结果对不上（可检测）。"""
    registry = _reg(Section("a", "原文。"))
    trace = capture_trace(PromptAssembler(registry), {})
    tampered = {**trace.to_dict(), "text": trace.text + "（被篡改）"}
    assert rebuild_text(tampered) != tampered["text"]
    assert rebuild_text(tampered) == trace.text  # 重建忠实于模板，不忠实于 text 字段


def test_tampered_template_is_detected():
    """改掉某一节的模板 → 重建出的文本与记录文本不一致（可检测）。"""
    registry = _reg(Section("a", "原文。"), Section("b", "第二节。"))
    trace = capture_trace(PromptAssembler(registry), {})
    data = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
    data["sections"][0]["template"] = "被换掉的模板。"
    assert rebuild_text(data) != data["text"]


def test_missing_variable_in_record_is_loud():
    """记录里丢了变量 → 重建 fail loud（不静默渲染出错误文本）。"""
    registry = _reg(Section("env", "{{cwd}}/{{time}}"))
    trace = capture_trace(PromptAssembler(registry), {"cwd": "w", "time": "t"})
    broken = json.loads(json.dumps(trace.to_dict()))
    broken["variables"].pop("time")
    with pytest.raises(ValueError, match="未知变量：time"):
        rebuild_text(broken)


def test_rebuild_does_not_need_original_registry():
    """重建只依赖记录数据：原注册表/作用域已被丢弃，照样重建。"""
    registry = _reg(Section("role", "基础。"))
    scope = registry.scoped("s")
    scope.register(Section("role", "覆盖。"))
    trace = capture_trace(PromptAssembler(scope), {})

    del registry, scope  # 丢弃进程内的原结构
    assert rebuild_text(trace) == "覆盖。"
