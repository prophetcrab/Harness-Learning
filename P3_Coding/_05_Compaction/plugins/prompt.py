"""prompt 插件 —— 提示词渲染服务（`prompt` 服务）。

把 M4 的三个构件（section 注册表 / 变量插值 / 装配单）接成"可反复调用的
渲染器"，并在**每次渲染前**经过 `prompt/variables` 瀑布——这就是其它插件
贡献运行时变量的地方（例如"注入固定时钟"）。

    ctx.on("prompt/variables", lambda value, **kw: {**value, "time": "固定"})

服务的形状与 `PromptRenderer` 一致（`render()` / `render_traced()`），
因此 agent 插件可以直接把它交给 `RuntimePromptProvider`（每 step 渲染）。
"""

from __future__ import annotations

from pathlib import Path

from components.prompt import (
    PromptAssembler,
    PromptRenderer,
    PromptTrace,
    RuntimeContext,
    collect_runtime_context,
    default_assembler,
)
from kernel import Context
from plugins.events import PROMPT_VARIABLES
from plugins.services import PROMPT_SERVICE


class PromptService(PromptRenderer):
    """渲染器 + 事件瀑布：每次渲染前让其它插件改写变量表。"""

    def __init__(self, assembler: PromptAssembler, workspace: str | Path, ctx: Context) -> None:
        self._workspace_path = Path(workspace).resolve()
        self._ctx = ctx
        super().__init__(assembler, self._sample)

    def _sample(self) -> RuntimeContext:
        """采样运行时上下文，并让 `prompt/variables` 瀑布改写变量表。"""
        base = collect_runtime_context(self._workspace_path)
        variables = self._ctx.waterfall(
            PROMPT_VARIABLES, base.variables(), workspace=str(self._workspace_path)
        )
        return RuntimeContext(
            cwd=str(variables.get("cwd", base.cwd)),
            platform=str(variables.get("platform", base.platform)),
            time=str(variables.get("time", base.time)),
            date=str(variables.get("date", base.date)),
        )

    # 显式覆写只为把返回类型说清楚（行为与基类一致：capture_trace + 瀑布变量）。
    def render_traced(self) -> PromptTrace:
        return super().render_traced()


class PromptPlugin:
    """提供 `prompt` 服务（含 section 装配器与运行时变量瀑布）。"""

    name = "prompt"
    #: 应用角色（base）：系统提示词是 agent 的组成部分。
    category = "base"

    def __init__(
        self,
        *,
        workspace: str | Path,
        assembler: PromptAssembler | None = None,
    ) -> None:
        self._workspace = workspace
        self._assembler = assembler

    def setup(self, ctx: Context) -> None:
        service = PromptService(
            self._assembler if self._assembler is not None else default_assembler(),
            self._workspace,
            ctx,
        )
        ctx.provide(PROMPT_SERVICE, service)


__all__ = ["PromptPlugin", "PromptService"]
