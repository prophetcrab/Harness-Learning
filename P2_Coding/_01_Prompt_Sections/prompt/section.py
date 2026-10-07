"""Section —— 系统提示词的"一节"。

这是 M4 引入的最小词汇：一份系统提示词不再是一整个写死的字符串，而是由若干
**有序的 section** 拼装而成。每个 section 是一个独立、可命名、可替换的单元。

为什么需要"名字"：有了名字才能**按名字遮蔽**——作用域里注册一个同名 section
就能覆盖基础层的那一节，而不必改动基础层（对应铁律 #4「注册表分层 + 遮蔽」）。

为什么需要"来源"：M4 的最终目标是"提示词可追溯、可重建"。`source` 记录这一节是
谁贡献的（内置 / 某个作用域 / 某个插件），供后续 `--dump-prompt` 展示来源链。

本模块只描述 section 长什么样；注册、遮蔽、装配分别在 registry.py 与 assembler.py。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """系统提示词里的一节。

    字段：
        name     这一节的唯一标识（用于注册与遮蔽）。非空。
        content  这一节的正文（将按注册顺序拼接成完整提示词）。
        source   来源标签（谁贡献的），默认 "builtin"。仅用于追溯，不参与装配。
        title    人类可读的小标题（可选；供 dump/调试展示，不进入装配结果）。

    frozen=True：section 一旦构造就是不可变的——注册进注册表后不会被外部改写。
    需要"改内容"时应注册一个**新的** Section（同名则遮蔽），而不是就地修改。
    """

    name: str
    content: str
    source: str = "builtin"
    title: str = ""

    def __post_init__(self) -> None:
        # fail loud：名字是唯一标识，空名字无法注册/遮蔽，属于调用方错误，立刻报错。
        if not self.name:
            raise ValueError("Section.name 不能为空")


__all__ = ["Section"]
