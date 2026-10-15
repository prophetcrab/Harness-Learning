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
from components.shell import DisplaySpec, RouteRequest, RouteSpec, Surface
from kernel import Context
from plugins.events import COMPACT_EVENT, LOOP_EVENT
from plugins.services import (
    AGENT_SERVICE,
    COMPACT_SURFACE,
    COMPACTER_SERVICE,
    LLM_SERVICE,
)

# ---------------------------------------------------------------------------
# 界面自述：我产出的事件怎么画、我的端点长什么样
# ---------------------------------------------------------------------------

_APPLIED_TITLE = "上下文已压缩：折叠 {folded} 条（约 {before_tokens} → {after_tokens} tokens）"

#: 事件的显示规则（壳按 renderer 分派，模板里 `{字段}` 由壳填）。
#:
#: `compact/checkpoint` 与 `compact_applied` 是**两种东西**：前者是进日志的会话
#: 事件（重放历史时也要画），后者是实时的循环事件。界面上长得一样，共用模板——
#: 但 kind 必须分开登记。
DISPLAY: tuple[DisplaySpec, ...] = (
    DisplaySpec("compact/checkpoint", "fold", _APPLIED_TITLE, "summary", order=10),
    DisplaySpec("compact_applied", "fold", _APPLIED_TITLE, "summary", order=11),
    DisplaySpec("compact_failed", "warn", "压缩未成功（已跳过本次）：{error}", order=12),
    DisplaySpec(
        "__compact_done", "status",
        "✓ 压缩完成：折叠 {folded} 条（约 {before_tokens} → {after_tokens} tokens）",
        order=13, terminal=True,
    ),
    DisplaySpec(
        "__compact_noop", "status", "没有可压缩的历史（只有当前一轮）",
        order=14, terminal=True,
    ),
)


def _compact_route() -> RouteSpec:
    """本插件的专属端点：一次手动压缩（NDJSON 流）。

    **handler 完全不碰 HTTP**：参数从 `request.params` 取、帧往 `request.stream()`
    写——分块传输、CRLF 分帧、终止块都由壳负责（那几处最容易写错，实测踩过三次）。
    """

    def handler(request: RouteRequest) -> None:
        session_id = request.params["id"]
        agent = request.ctx.get(AGENT_SERVICE)
        if agent is None:
            request.json({"error": "agent 服务未就绪（agent 插件未激活）"}, status=503)
            return
        stream = request.stream()
        try:
            try:
                outcome = agent.compact(session_id)
            except Exception as exc:      # 手动动作不静默：失败要说给人听
                stream.frame("__error", {"message": f"压缩失败：{exc}"})
                return
            if outcome is None:
                stream.frame("__compact_noop", {"message": "没有可压缩的历史（只有当前一轮）"})
            else:
                stream.frame(
                    "__compact_done",
                    {
                        "folded": outcome.folded,
                        "before_tokens": outcome.before_tokens,
                        "after_tokens": outcome.after_tokens,
                        "summary_tokens": outcome.summary_tokens,
                    },
                )
        finally:
            stream.close()

    return RouteSpec("POST", "/api/sessions/<id>/compact", handler, order=10)


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
        # 界面自述（壳不认识本插件，只读这些声明）：
        #   display —— 我产出的事件该怎么画（四种通用渲染器之一）
        #   routes  —— 我的专属端点（壳负责挂载与传输）
        ctx.provide(
            COMPACT_SURFACE,
            Surface(display=DISPLAY, routes=(_compact_route(),)),
        )

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
                    trigger=data.get("trigger", "auto"),
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
