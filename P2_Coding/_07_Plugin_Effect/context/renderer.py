"""PromptRenderer —— 把"装配器 + 运行时上下文来源"绑成可反复调用的渲染器。

_01 的装配器是静态的；_02 把"渲染"变成**可反复调用的动作**：每调用一次 `render()`，
就重新采样一次运行时上下文（时间会走、cwd 可能变），把最新的变量值插进 section 再拼接。
_03 再进一步：`render_traced()` 返回**装配单**（render 的文本只是它的一个字段）——
渲染的每一个组成部分都留在记录里，事后可由日志重建。

职责边界：
- 装配器保持纯函数（吃字符串吐字符串）——同样的 section + 同样的变量值 → 同样的文本；
- "什么时候重新采样/重新渲染"是运行时策略，属于本类；每 step 调一次 `render_traced()`，
  就是"每 step 渲染"的实现（挂钩点见 provider.py）。
"""

from __future__ import annotations

from collections.abc import Callable

from context.runtime import RuntimeContext
from prompt.assembler import PromptAssembler
from prompt.trace import PromptTrace, capture_trace

# 上下文来源：每次调用返回一份"当下"的快照（真实运行 = collect_runtime_context）。
ContextSource = Callable[[], RuntimeContext]


class PromptRenderer:
    """可重复调用的渲染器：render_traced() = 采样上下文 → 插值 → 拼接 + 记装配单。"""

    def __init__(self, assembler: PromptAssembler, source: ContextSource) -> None:
        self._assembler = assembler
        self._source = source

    @property
    def assembler(self) -> PromptAssembler:
        """底层装配器（只读）——parts() 等静态结构从这里取。"""
        return self._assembler

    def render_traced(self) -> PromptTrace:
        """按"当下"的上下文渲染一次，并产出完整装配单（变量在此刻取值）。"""
        return capture_trace(self._assembler, self._source().variables())

    def render(self) -> str:
        """按"当下"的上下文渲染一次完整系统提示词（装配单的 text 字段）。"""
        return self.render_traced().text


__all__ = ["ContextSource", "PromptRenderer"]
