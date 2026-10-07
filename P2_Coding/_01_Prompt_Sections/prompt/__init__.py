"""prompt —— M4 第一步：系统提示词的 section 注册表与装配（本阶段新增的顶层模块）。

**位置说明（P2 组织约定）**：本包放在**阶段主目录的顶层**（`_01_Prompt_Sections/prompt/`），
不在 `harness/` 里。约定是：`harness/` 冻结为 M1–M3 基线库，本阶段（及后续阶段）新增的
机制作为**顶层模块**与 `harness/` 平级摆放——这样"这一阶段加了什么"一眼可见。

本阶段引入的最小机制，一句话：

    系统提示词不是"一个写死的字符串"，而是**若干有序 section 装配出来的产物**。

组件：
- `Section`          一节提示词：name（可遮蔽）+ content + source（可追溯）；
- `SectionRegistry`  有序的基础注册表；重复注册报错（fail loud）；
- `SectionScope`     作用域覆盖层：同名遮蔽、未同名追加、撤下后基础层自动恢复（铁律 #4）；
- `PromptAssembler`  按来源顺序把 section 拼成完整文本（分隔符可配）；
- `defaults`         内置默认 section（role / tools / style）与默认装配器。

本包是**纯逻辑**：不依赖 harness，只吃字符串、吐字符串，因此可被任何地方引用。
装配点是本阶段的入口（`demo.py` 与测试）：构造装配器 → `assembler.assemble()` →
把得到的字符串交给 `harness.mini.MiniHarness.open(system_prompt=...)`（harness 本阶段未改）。

下一阶段 `_02_Prompt_Context` 会在此基础上引入变量插值与"每 step 渲染运行时上下文"。
"""

from prompt.assembler import PromptAssembler, SectionSource
from prompt.defaults import default_assembler, default_registry, default_sections
from prompt.registry import SectionRegistry, SectionScope
from prompt.section import Section

__all__ = [
    # 词汇
    "Section",
    # 注册表 + 作用域
    "SectionRegistry",
    "SectionScope",
    # 装配
    "PromptAssembler",
    "SectionSource",
    # 内置默认
    "default_sections",
    "default_registry",
    "default_assembler",
]
