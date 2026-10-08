"""PromptAssembler —— 把有序 section 拼成完整的系统提示词（含变量插值）。

M4 的第二个机制：**装配器**。它持有一个"section 来源"（基础注册表，或某个作用域
覆盖层），按该来源给定的顺序把各节正文拼成一段文本。

本阶段（`_02`）对它的最小扩展：`assemble(variables)` 在拼接时顺便做 `{{name}}`
插值（见 interpolate.py）。于是同一个装配器可以**反复渲染**：静态 section 逐字节
稳定，带变量的 section 每次按当下取值。谁负责"什么时候重新取值"？不在装配器里
（它保持纯函数）——见 `context/renderer.py` 的 PromptRenderer。

其余设计与 _01 相同：
- 注册表只管"有哪些 section、什么顺序"，装配器只管"怎么拼成文本"（分隔符等）；
- 装配器对来源是**只读**的：只读取 `.sections()`，因此基础注册表与作用域覆盖层
  都能作为来源；
- `parts()` 是装配的"配料表"（含遮蔽效果），后续 `_03_Prompt_Trace` 的
  `--dump-prompt` 会消费它。
"""

from __future__ import annotations

from collections.abc import Mapping

from prompt.interpolate import render_text
from prompt.registry import SectionRegistry, SectionScope
from prompt.section import Section

# 装配器的来源：基础注册表或作用域覆盖层，二者都提供 .sections() / .names。
SectionSource = SectionRegistry | SectionScope


class PromptAssembler:
    """按顺序把 section 正文拼成系统提示词（可选变量插值）。

    参数：
        source      section 来源（SectionRegistry 或 SectionScope）。默认空注册表。
        separator   拼接用的分隔符，默认空行（"\\n\\n"）——让多节读起来是自然的分段。

    用法：
        registry = SectionRegistry()
        registry.register(Section("role", "你是一名助手。"))
        registry.register(Section("env", "工作目录：{{cwd}}。"))
        PromptAssembler(registry).assemble({"cwd": "D:/ws"})
        # -> "你是一名助手。\\n\\n工作目录：D:/ws。"
    """

    def __init__(self, source: SectionSource | None = None, *, separator: str = "\n\n") -> None:
        self._source: SectionSource = source if source is not None else SectionRegistry()
        self._separator = separator

    @property
    def source(self) -> SectionSource:
        """当前的 section 来源（只读）。"""
        return self._source

    @property
    def separator(self) -> str:
        """拼接用的分隔符（只读）——装配单（prompt/trace.py）要把它记进日志。"""
        return self._separator

    def parts(self) -> list[Section]:
        """即将参与装配的 section（已含作用域遮蔽效果），按装配顺序。

        这是装配的"配料表"：`_03` 的 `--dump-prompt` 与单元测试都从这里取顺序与来源。
        注意 parts() 给出的是**模板**（{{name}} 尚未替换）；渲染结果见 assemble()。
        """
        return self._source.sections()

    def assemble(self, variables: Mapping[str, str] | None = None) -> str:
        """按顺序拼接各节正文；正文里的 {{name}} 用 variables 插值。

        - 没有占位符的 section 与非空 variables 无关，逐字节不变；
        - 有占位符但 variables 缺失对应键 → fail loud（未知变量）；
        - variables 省略等价于空表：适合纯静态 section。
        """
        provided = dict(variables or {})
        return self._separator.join(
            render_text(section.content, provided) for section in self.parts()
        )

    def __str__(self) -> str:
        # 方便直接 f"{assembler}" 拿到成品文本；含变量的装配需要显式传 variables。
        return self.assemble()


__all__ = ["PromptAssembler", "SectionSource"]
