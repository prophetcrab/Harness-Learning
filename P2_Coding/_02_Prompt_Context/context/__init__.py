"""context —— 运行时上下文与"每 step 渲染"（阶段 `_02` 新增的顶层模块）。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级，基线一行未改。
本阶段机制一句话：

    系统提示词里的 {{cwd}} / {{platform}} / {{time}} 在**每一 step** 渲染时求值，
    渲染结果作为 system/message 事件进日志；模型每一步看到的都是当时的最新渲染。

组件：
- `RuntimeContext` / `collect_runtime_context`  一次渲染时刻的环境快照（runtime.py）
- `PromptRenderer`                              装配器 + 上下文来源 → 可反复渲染（renderer.py）
- `RuntimePromptProvider`                       provider 包装：每 step 渲染/记录/改写请求（provider.py）
- `ContextSession`                              扩展事件词汇（system/message）+ 遮蔽投影（session.py）
- `project` / `effective_system_prompt`         扩展投影（projection.py）
- `open_context_harness` / `ContextHarness`     把以上接成一条会话（wiring.py）

依赖方向：`context → prompt`（插值与装配）+ `context → harness`（会话/循环/工具）。
"""

from context.projection import effective_system_prompt, project
from context.provider import RenderSink, RuntimePromptProvider, with_system_prompt
from context.renderer import ContextSource, PromptRenderer
from context.runtime import RuntimeContext, collect_runtime_context
from context.session import EXTRA_EVENT_TYPES, ContextSession
from context.wiring import ContextHarness, open_context_harness

__all__ = [
    # 运行时上下文
    "RuntimeContext",
    "collect_runtime_context",
    # 渲染
    "PromptRenderer",
    "ContextSource",
    # provider 包装
    "RuntimePromptProvider",
    "RenderSink",
    "with_system_prompt",
    # 会话扩展
    "ContextSession",
    "EXTRA_EVENT_TYPES",
    # 投影
    "project",
    "effective_system_prompt",
    # 接线
    "ContextHarness",
    "open_context_harness",
]
