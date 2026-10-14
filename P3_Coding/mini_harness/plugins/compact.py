"""compact 插件 —— 上下文压缩（P3 `_05` 的接线）。

机制本体在 `components/agent/compact.py`（策略 / 切点 / 摘要请求 / Compactor）。
本插件只做三件事：

1. **读配置**：容量取自 `--context-window`（默认 1M，见 components/context/tokens.py）；
   三个比例（触发 90% / 保留 10% / 摘要 5%）在 `CompactionPolicy` 里，需要时
   入口可以直接构造策略再装本插件的工厂。
2. **提供 `compacter` 服务**：`ctx.provide(COMPACTER_SERVICE, ...)`——agent 插件
   注入它，把 Compactor 交给每条会话的 AgentLoop（换插件 = 换压缩策略，
   甚至卸载 = 完全不压）。
3. **留痕**：装载后订阅主循环事件，把 `compact_applied` / `compact_failed`
   翻成内核事件 `context/compacted`，供界面显示"压缩发生了"。

**与 llm 插件的顺序无关**：本插件的 setup 里 `ctx.require(LLM_SERVICE)` 取的是
**当前**的 provider——`inject` 声明保证 llm 先激活；llm 之后被卸载重装，
agent 插件会跟着重新装配（inject 生命周期），但 `Compactor` 里握着的 provider
引用需要跟着换，所以本插件也 inject LLM_SERVICE，靠"卸载 → 重装"刷新。

摘要调用本身也走 `llm` 服务（瀑布与 llm/response 事件都经过），所以它会：
- 被 token 账本记一笔（`_03`：摘要也是真金白银）；
- 被仪表看到（`_04`：摘要请求的构成也会进四类读数）。
这是刻意的——压缩的代价必须可见，不能偷偷花。
"""

from __future__ import annotations

from components.agent.compact import CompactionPolicy, Compactor
from components.context.tokens import DEFAULT_CONTEXT_WINDOW
from kernel import Context
from plugins.events import COMPACT_EVENT, LOOP_EVENT
from plugins.services import COMPACTER_SERVICE, LLM_SERVICE


class CompactPlugin:
    """提供 `compacter` 服务（inject llm）；装载即生效，卸载即不再压缩。"""

    name = "compact"
    inject = (LLM_SERVICE,)

    def __init__(
        self,
        *,
        policy: CompactionPolicy | None = None,
        capacity: int = DEFAULT_CONTEXT_WINDOW,
    ) -> None:
        if policy is not None and capacity != DEFAULT_CONTEXT_WINDOW:
            raise ValueError("policy 与 capacity 只能给一个（policy 里已含容量）")
        self._policy = policy if policy is not None else CompactionPolicy(capacity=capacity)

    @property
    def policy(self) -> CompactionPolicy:
        return self._policy

    def setup(self, ctx: Context):
        compactor = Compactor(ctx.require(LLM_SERVICE), self._policy)
        ctx.provide(COMPACTER_SERVICE, compactor)

        def on_loop_event(**payload) -> None:
            """把压缩的循环事件翻成内核事件（界面据此显示"压缩发生了"）。"""
            kind = payload.get("kind")
            data = payload.get("payload") or {}
            if kind == "compact_applied":
                ctx.emit(
                    COMPACT_EVENT,
                    session=payload.get("session", ""),
                    status="applied",
                    folded=data.get("folded", 0),
                    before_tokens=data.get("before_tokens", 0),
                    after_tokens=data.get("after_tokens", 0),
                    summary_tokens=data.get("summary_tokens", 0),
                    turn=data.get("turn", 0),
                    step=data.get("step", 0),
                )
            elif kind == "compact_failed":
                ctx.emit(
                    COMPACT_EVENT,
                    session=payload.get("session", ""),
                    status="failed",
                    error=data.get("error", ""),
                    turn=data.get("turn", 0),
                    step=data.get("step", 0),
                )

        # 装载期订阅 → 注册即 effect：卸载自动退订
        ctx.on(LOOP_EVENT, on_loop_event)
        return None


def compact_plugin(
    *,
    policy: CompactionPolicy | None = None,
    capacity: int = DEFAULT_CONTEXT_WINDOW,
) -> CompactPlugin:
    """工厂：给容量（用默认比例）或给完整策略（自己定阈值）。"""
    return CompactPlugin(policy=policy, capacity=capacity)


__all__ = ["CompactPlugin", "compact_plugin"]
