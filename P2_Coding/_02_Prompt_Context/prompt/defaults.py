"""默认 section 集合 —— 把原先那串写死的系统提示词拆成有序的几节。

_01 拆出三节（role / tools / style）；本阶段（`_02`）追加一节 **env**：
它引用运行时上下文 `{{cwd}}` / `{{platform}}` / `{{time}}`，是"每 step 渲染"
最直观的载体——同一份 section 模板，不同时刻渲染出的文本不同。

    role    角色设定
    tools   工具使用约定
    env     运行时环境（cwd / platform / time——本阶段新增）
    style   回答风格

注意：env 一节带占位符，因此用默认装配器渲染时必须提供变量
（`default_assembler().assemble(variables)`）；缺变量 fail loud 是刻意行为。
真实运行/演示里，变量由 `context.collect_runtime_context()` 采集。

约定：内置 section 的 `source` 留空（默认 "builtin"）；进入某个作用域时，作用域会
把来源改写成该作用域的名字，便于追溯（见 registry.SectionScope.register）。
"""

from __future__ import annotations

from prompt.assembler import PromptAssembler
from prompt.registry import SectionRegistry
from prompt.section import Section


def default_sections() -> list[Section]:
    """内置的默认 section，按装配顺序返回（返回新列表，调用方可自由改动）。"""
    return [
        Section(
            name="role",
            title="角色设定",
            content="你是一只猫娘，可以调用工具。",
        ),
        Section(
            name="tools",
            title="工具使用约定",
            content=(
                "涉及算术计算时必须调用 calculate 工具，不要心算；"
                "需要读写文件时使用 read_file / write_file / list_files，"
                "路径用相对工作区根目录的相对路径。"
            ),
        ),
        Section(
            name="env",
            title="运行时环境",
            content="运行时环境：工作区根目录 {{cwd}}；操作系统 {{platform}}；当前时间 {{time}}。",
        ),
        Section(
            name="style",
            title="回答风格",
            content="最终回答用中文，简洁。",
        ),
    ]


def default_registry() -> SectionRegistry:
    """构造只含内置 section 的基础注册表。"""
    registry = SectionRegistry()
    for section in default_sections():
        registry.register(section)
    return registry


def default_assembler() -> PromptAssembler:
    """构造使用内置 section 的默认装配器（渲染时需提供 env 一节的变量）。"""
    return PromptAssembler(default_registry())


__all__ = ["default_sections", "default_registry", "default_assembler"]
