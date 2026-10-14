"""提示词构件 —— section 注册表 + 作用域遮蔽 + 插值 + 装配 + 装配单。

M4（P2 `_01`–`_03`）的全部纯逻辑，原样沿用：吃字符串、吐字符串，
不依赖 agent / 会话 / 插件任何一层。运行时上下文采集（runtime.py）也在本包。
"""

from components.prompt.assembler import PromptAssembler
from components.prompt.defaults import default_assembler, default_registry, default_sections
from components.prompt.interpolate import referenced_names, render_text
from components.prompt.provider import RuntimePromptProvider, with_system_prompt
from components.prompt.registry import SectionRegistry, SectionScope
from components.prompt.renderer import ContextSource, PromptRenderer
from components.prompt.runtime import RuntimeContext, collect_runtime_context
from components.prompt.section import Section
from components.prompt.trace import (
    PromptTrace,
    SectionTrace,
    capture_trace,
    rebuild_text,
)

__all__ = [
    "ContextSource",
    "PromptAssembler",
    "PromptRenderer",
    "PromptTrace",
    "RuntimeContext",
    "RuntimePromptProvider",
    "Section",
    "SectionRegistry",
    "SectionScope",
    "SectionTrace",
    "capture_trace",
    "collect_runtime_context",
    "default_assembler",
    "default_registry",
    "default_sections",
    "rebuild_text",
    "referenced_names",
    "render_text",
    "with_system_prompt",
]
