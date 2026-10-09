"""prompt —— 系统提示词的 section 结构 + 插值 + 装配单（阶段 `_03` 的顶层模块）。

**位置说明（P2 组织约定）**：本包放在**阶段主目录的顶层**（`_03_Prompt_Trace/prompt/`），
与 `harness/` 平级；`harness/` 冻结为 M1–M3 基线库，本阶段一行未改。

本包目前的机制（累计推进）：
- `_01`：系统提示词不是"一个写死的字符串"，而是**若干有序 section 装配出来的产物**
  （Section / SectionRegistry / SectionScope / PromptAssembler）；
- `_02`：section 里的 `{{name}}` 在**渲染时**求值（interpolate.render_text），
  装配器 `assemble(variables)` 因此可反复渲染、每次按当下取值；
- `_03`：每次渲染可生成**装配单**（capture_trace）——记录每节的来源/模板/引用变量
  与最终文本；`rebuild_text()` 由记录重建文本，即"日志 + 装配器 == 当时的提示词"。

组件：
- `Section`          一节提示词：name（可遮蔽）+ content + source（可追溯）；
- `SectionRegistry`  有序的基础注册表；重复注册报错（fail loud）；
- `SectionScope`     作用域覆盖层：同名遮蔽、未同名追加、撤下后基础层自动恢复（铁律 #4）；
- `PromptAssembler`  按来源顺序把 section 拼成完整文本（separator 可配；variables 插值）；
- `render_text`      {{name}} 插值的最小实现（未知/非法变量 fail loud）；
- `PromptTrace`      装配单：一次渲染的完整记录（可序列化 / 可重建）；
- `capture_trace`    渲染一次并产出装配单；`rebuild_text` 由记录重建文本；
- `defaults`         内置默认 section（role / tools / env / style）与默认装配器。

本包是**纯逻辑**：不依赖 harness、不依赖 context，只吃字符串、吐字符串。
"每 step 重新采样上下文 → 渲染 → 装配单进日志"的运行时机制在顶层 `context/` 包里。
"""

from prompt.assembler import PromptAssembler, SectionSource
from prompt.defaults import default_assembler, default_registry, default_sections
from prompt.interpolate import referenced_names, render_text
from prompt.registry import SectionRegistry, SectionScope
from prompt.section import Section
from prompt.trace import PromptTrace, SectionTrace, capture_trace, rebuild_text

__all__ = [
    # 词汇
    "Section",
    # 注册表 + 作用域
    "SectionRegistry",
    "SectionScope",
    # 装配（+ 插值）
    "PromptAssembler",
    "SectionSource",
    "render_text",
    "referenced_names",
    # 装配单（+ 重建）
    "PromptTrace",
    "SectionTrace",
    "capture_trace",
    "rebuild_text",
    # 内置默认
    "default_sections",
    "default_registry",
    "default_assembler",
]
