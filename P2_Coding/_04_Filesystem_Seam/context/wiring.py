"""接线 —— 把"提示词渲染 + 扩展日志 + 包装 provider"装成一条可运行会话。

入口脚本（chat.py / demo.py / 测试）用它，而不是 `MiniHarness.open()`。
组装视图：

    PromptRenderer(assembler, context_source)              # 每步可渲染（含装配单）
        └─► RuntimePromptProvider(inner, renderer, on_render=…)   # 每步挂钩
                └─► MiniHarness(session, provider, registry, …)   # 基线组装，未改
    ContextSession 作为会话本体（扩展 system/message 事件 + 遮蔽投影）

会话开场约定：新会话把"开场那一刻的渲染 + 装配单"写进 session/start（第 0 步快照）；
resume 时以日志里的当前生效文本作为"已记录"基线——渲染没变化就不追加事件。
`_03` 起：每次因变化而追加的 system/message 都携带完整装配单，事后可由日志重建。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from context.projection import effective_system_prompt
from context.provider import RuntimePromptProvider
from context.renderer import ContextSource, PromptRenderer
from context.runtime import collect_runtime_context
from context.session import ContextSession
from harness.llm.provider import LLMProvider
from harness.mini import MiniHarness, Presenter
from harness.session import JsonlStore, RepairReport
from harness.tools import ApprovalPolicy, ToolMiddleware, ToolRegistry
from harness.tools.workspace import build_workspace_registry
from prompt import PromptAssembler, default_assembler


@dataclass
class ContextHarness:
    """一条"带运行时上下文"的会话：基线 harness + 本阶段机制的部件。"""

    harness: MiniHarness
    session: ContextSession
    renderer: PromptRenderer
    provider: RuntimePromptProvider
    report: RepairReport


def open_context_harness(
    session_id: str,
    *,
    provider: LLMProvider,
    root: str | Path,
    workspace: str | Path | None = None,
    assembler: PromptAssembler | None = None,
    context_source: ContextSource | None = None,
    approval: ApprovalPolicy | None = None,
    include_search: bool = False,
    tool_registry: ToolRegistry | None = None,
    timeout: float = 30.0,
    max_steps: int = 8,
    middlewares: Sequence[ToolMiddleware] = (),
    on_event: Presenter | None = None,
) -> ContextHarness:
    """打开（或恢复）一条会话，并把"每 step 渲染系统提示词"接上。

    - assembler：渲染用的 section 来源（默认内置 role/tools/env/style 四节）；
    - context_source：运行时上下文来源（默认为 workspace 的实时采集）；
    - provider：内层真实 provider，会被 RuntimePromptProvider 包装后交给基线；
    - tool_registry：工具面。缺省用基线的 `build_workspace_registry(workspace)`；
      `_04` 起可传入由能力接缝组装的注册表（如 `providers.build_filesystem_registry`），
      工具实现因此可整体替换（local / memory / …）。
    """
    workspace_path = Path(workspace) if workspace is not None else Path(root)
    source = context_source or (lambda: collect_runtime_context(workspace_path))
    renderer = PromptRenderer(assembler or default_assembler(), source)

    store = JsonlStore(Path(root))
    events, report = store.load(session_id)
    session = ContextSession(session_id, store=store, events=events)

    if session.has_started:
        # resume：日志里已有"当前生效文本"，作为去重基线（没变就不重复记）。
        baseline = effective_system_prompt(session.events)
    else:
        trace = renderer.render_traced()
        baseline = trace.text
        session.append(
            "session/start",
            {
                "session_id": session_id,
                "system_prompt": baseline,
                "prompt_trace": trace.to_dict(),  # ★ 开场装配单：文本的"来源记录"
            },
        )

    wrapped = RuntimePromptProvider(
        provider,
        renderer,
        on_render=lambda trace: session.append(
            "system/message",
            {"content": trace.text, "prompt_trace": trace.to_dict()},  # ★ 变更 + 装配单
        ),
        initial=baseline,
    )
    registry = (
        tool_registry
        if tool_registry is not None
        else build_workspace_registry(workspace_path, include_search=include_search)
    )
    harness = MiniHarness(
        session,
        wrapped,
        registry,
        approval=approval,
        timeout=timeout,
        max_steps=max_steps,
        middlewares=middlewares,
        on_event=on_event,
    )
    return ContextHarness(
        harness=harness,
        session=session,
        renderer=renderer,
        provider=wrapped,
        report=report,
    )


__all__ = ["ContextHarness", "open_context_harness"]
