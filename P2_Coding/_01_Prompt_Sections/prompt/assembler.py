"""PromptAssembler —— 把有序 section 拼成完整的系统提示词。

M4 的第二个机制：**装配器**。它持有一个"section 来源"（基础注册表，或某个作用域
覆盖层），按该来源给定的顺序把各节正文拼成一段文本。

为什么把"装配"单独成一层：
- 注册表只管"有哪些 section、什么顺序"，装配器只管"怎么拼成文本"（分隔符等）；
- 于是同一份注册表可以有不同的装配策略（分隔符、前缀），而注册内容不变；
- 后续 `_03_Prompt_Trace` 要展示"这一节来自哪里"，正好复用本组装器的 `parts()`。

装配器对来源是**只读**的：它不注册、不改顺序，只读取 `.sections()`。因此它既能接
基础注册表，也能接一个作用域覆盖层（两者都提供 `.sections()`），从而支持"作用域
生效时装配出的提示词"。
"""

from __future__ import annotations

from prompt.registry import SectionRegistry, SectionScope
from prompt.section import Section

# 装配器的来源：基础注册表或作用域覆盖层，二者都提供 .sections() / .names。
SectionSource = SectionRegistry | SectionScope


class PromptAssembler:
    """按顺序把 section 正文拼成系统提示词。

    参数：
        source      section 来源（SectionRegistry 或 SectionScope）。默认空注册表。
        separator   拼接用的分隔符，默认空行（"\\n\\n"）——让多节读起来是自然的分段。
                    section 正文本身不带首尾空白时，拼接结果最稳定。

    用法：
        registry = SectionRegistry()
        registry.register(Section("role", "你是一名助手。"))
        registry.register(Section("style", "回答简洁。"))
        PromptAssembler(registry).assemble()
        # -> "你是一名助手。\\n\\n回答简洁。"
    """

    def __init__(self, source: SectionSource | None = None, *, separator: str = "\n\n") -> None:
        self._source: SectionSource = source if source is not None else SectionRegistry()
        self._separator = separator

    @property
    def source(self) -> SectionSource:
        """当前的 section 来源（只读）。"""
        return self._source

    def parts(self) -> list[Section]:
        """即将参与装配的 section（已含作用域遮蔽效果），按装配顺序。

        这是装配的"配料表"：`_03` 的 `--dump-prompt` 与单元测试都从这里取顺序与来源。
        """
        return self._source.sections()

    def assemble(self) -> str:
        """按顺序拼接各节正文，返回完整的系统提示词。"""
        return self._separator.join(section.content for section in self.parts())

    def __str__(self) -> str:
        # 方便直接 f"{assembler}" 拿到成品文本。
        return self.assemble()


__all__ = ["PromptAssembler", "SectionSource"]
