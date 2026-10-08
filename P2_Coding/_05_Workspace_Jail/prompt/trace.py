"""装配单（trace）—— 记录"这份提示词是怎么拼出来的"，并支持由记录重建。

M4 第三步引入的词汇。前两步解决了"结构"（`_01`：section 注册表）与"动态"
（`_02`：变量插值 + 每 step 渲染），但**没留下过程**：日志里只有一段文本，
事后无法回答"这一节从哪来、当时变量取什么值"。本阶段补上这一点。

每次渲染生成一份 **PromptTrace（装配单）**——不是只留最终文本，而是留"配料表"：

- 每一节：name / source（谁贡献的）/ title / 模板（未插值原文）/
  引用变量名 / 渲染文本；
- 整份：本次用到的变量取值、分隔符、最终文本。

装配单随 `session/start` / `system/message` 事件进日志（接线见 context/wiring）。
于是"可重建"的断言有了确切含义：

    rebuild_text(日志里的装配单) == 日志里记录的文本

重建刻意从**记录的数据**出发：新建注册表、把记录里的模板注册回去、用同一套
装配器拼接——不依赖进程里的原注册表、不依赖原作用域。记录缺了变量就 fail loud，
记录被改动过就重建不出原文（检验是有牙齿的，见 demo 第 0b 节）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from prompt.assembler import PromptAssembler
from prompt.interpolate import referenced_names, render_text
from prompt.registry import SectionRegistry
from prompt.section import Section


@dataclass(frozen=True)
class SectionTrace:
    """装配单里的一节：它是什么、从哪来、渲染成了什么。"""

    name: str
    source: str
    title: str
    template: str                    # 模板原文（{{name}} 未替换）
    rendered: str                    # 本次渲染的文本（插值后）
    referenced: tuple[str, ...] = ()  # 本节引用的变量名，按出现顺序


@dataclass(frozen=True)
class PromptTrace:
    """一次渲染的完整装配单（可序列化进日志）。"""

    sections: tuple[SectionTrace, ...]
    variables: dict[str, str]        # 本次渲染用到的变量取值
    separator: str
    text: str                        # 最终文本（= 各节 rendered 用 separator 拼接）

    # ------------------------------------------------------------------
    # 序列化（进日志 / 从日志读回）
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的普通字典（进事件负载）。"""
        return {
            "text": self.text,
            "separator": self.separator,
            "variables": dict(self.variables),
            "sections": [
                {
                    "name": entry.name,
                    "source": entry.source,
                    "title": entry.title,
                    "template": entry.template,
                    "rendered": entry.rendered,
                    "referenced": list(entry.referenced),
                }
                for entry in self.sections
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PromptTrace:
        """从日志里读回的字典还原装配单（缺字段用保守默认值）。"""
        sections = tuple(
            SectionTrace(
                name=entry["name"],
                source=entry.get("source", "builtin"),
                title=entry.get("title", ""),
                template=entry.get("template", ""),
                rendered=entry.get("rendered", ""),
                referenced=tuple(entry.get("referenced", ())),
            )
            for entry in raw.get("sections", [])
        )
        return cls(
            sections=sections,
            variables=dict(raw.get("variables", {})),
            separator=raw.get("separator", "\n\n"),
            text=raw.get("text", ""),
        )

    # ------------------------------------------------------------------
    # 重建
    # ------------------------------------------------------------------

    def rebuild(self) -> str:
        """由装配单重新走一遍装配：注册模板 → 插值 → 拼接。

        只使用装配单里记录的数据（不碰原注册表/作用域），返回重建出的文本。
        与 `self.text` 比对即可判定这份记录是否自洽（== "可由日志重建"）。
        """
        registry = SectionRegistry()
        for entry in self.sections:
            registry.register(
                Section(
                    name=entry.name,
                    content=entry.template,
                    source=entry.source,
                    title=entry.title,
                )
            )
        return PromptAssembler(registry, separator=self.separator).assemble(self.variables)


def capture_trace(
    assembler: PromptAssembler, variables: Mapping[str, str] | None = None
) -> PromptTrace:
    """按装配器当前来源渲染一次，并把全过程记成装配单。

    它做与 `assembler.assemble(variables)` 相同的渲染（逐节插值 + separator 拼接），
    只是顺带留下每节的模板、引用变量与渲染文本。未知变量等错误与 assemble 一致
    （fail loud，不产生半份记录）。
    """
    provided = dict(variables or {})
    entries = [
        SectionTrace(
            name=section.name,
            source=section.source,
            title=section.title,
            template=section.content,
            rendered=render_text(section.content, provided),
            referenced=tuple(referenced_names(section.content)),
        )
        for section in assembler.parts()
    ]
    text = assembler.separator.join(entry.rendered for entry in entries)
    return PromptTrace(
        sections=tuple(entries),
        variables=provided,
        separator=assembler.separator,
        text=text,
    )


def rebuild_text(trace: PromptTrace | Mapping[str, Any]) -> str:
    """由装配单（对象或从日志读回的字典）重建文本——"日志 + 装配器"的入口。"""
    data = trace if isinstance(trace, PromptTrace) else PromptTrace.from_dict(trace)
    return data.rebuild()


__all__ = ["SectionTrace", "PromptTrace", "capture_trace", "rebuild_text"]
