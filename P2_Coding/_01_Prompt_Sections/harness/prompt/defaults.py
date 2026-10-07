"""默认 section 集合 —— 把原先那串写死的系统提示词拆成有序的几节。

这是 M4 "装配"最直观的证据：改动这里就能看出系统提示词是**由 section 拼出来的**。
原先（P1）它是一整个字符串常量 `DEFAULT_SYSTEM_PROMPT`；现在它被拆成三节：

    role    角色设定
    tools   工具使用约定
    style   回答风格

用法上二者等价（`default_assembler().assemble()` 就是那段文本），差别在于：现在
可以**单独替换/遮蔽/追加**任意一节，而不必碰其它节——这正是 M4 想要的性质。

约定：内置 section 的 `source` 留空（默认 "builtin"）；进入某个作用域时，作用域会
把来源改写成该作用域的名字，便于追溯（见 registry.SectionScope.register）。
"""

from __future__ import annotations

from harness.prompt.assembler import PromptAssembler
from harness.prompt.registry import SectionRegistry
from harness.prompt.section import Section


def default_sections() -> list[Section]:
    """内置的默认 section，按装配顺序返回。

    返回新列表，调用方可自由改动（例如增删后再组装成自己的注册表）。
    """
    return [
        Section(
            name="role",
            title="角色设定",
            content="你是一名严谨的中文助手，可以调用工具。",
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
    """构造使用内置 section 的默认装配器（等价于原先的 DEFAULT_SYSTEM_PROMPT）。"""
    return PromptAssembler(default_registry())


__all__ = ["default_sections", "default_registry", "default_assembler"]
