"""PromptRenderer —— 把"装配器 + 运行时上下文来源"绑成可反复调用的渲染器。

_01 的装配器是静态的：`assemble()` 一次，文本永远一样。本阶段把"渲染"变成
**可反复调用的动作**：每调用一次 `render()`，就重新采样一次运行时上下文
（时间会走、cwd 可能变），把最新的变量值插进 section 再拼接。

职责边界：
- 装配器保持纯函数（吃字符串吐字符串）——同样的 section + 同样的变量值 → 同样的文本；
- "什么时候重新采样/重新渲染"是运行时策略，属于本类；每 step 调一次 `render()`，
  就是"每 step 渲染"的实现（挂钩点见 provider.py）。
"""

from __future__ import annotations

from collections.abc import Callable

from context.runtime import RuntimeContext
from prompt.assembler import PromptAssembler

# 上下文来源：每次调用返回一份"当下"的快照（真实运行 = collect_runtime_context）。
ContextSource = Callable[[], RuntimeContext]


class PromptRenderer:
    """可重复调用的渲染器：render() = 采样上下文 → 插值 → 拼接。"""

    def __init__(self, assembler: PromptAssembler, source: ContextSource) -> None:
        self._assembler = assembler
        self._source = source

    @property
    def assembler(self) -> PromptAssembler:
        """底层装配器（只读）——parts() 等静态结构从这里取。"""
        return self._assembler

    def render(self) -> str:
        """按"当下"的上下文渲染一次完整系统提示词（变量在此刻取值）。"""
        return self._assembler.assemble(self._source().variables())


__all__ = ["ContextSource", "PromptRenderer"]
