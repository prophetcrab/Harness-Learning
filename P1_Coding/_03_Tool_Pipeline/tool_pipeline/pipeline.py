"""工具执行管线 —— pre → execute → post 三段式，用"中间件链"实现。

为什么用管线，而不是在主循环里散落一堆 if（决策记录 0003 详解）：

    classify(危险？) → 校验参数 → 审批 → 执行 → 超时 → 截断输出 → 脱敏…

这些关注点如果写成 if，会长进循环里、彼此耦合、无法单独测试、也无法按会话
定制。抽成三段式后，每一段都是"一串中间件"，各自只管一件事：

    pre      决策：allow / deny / ask；可改写参数（如补默认值、规范化路径）
    execute  真正执行工具，含超时
    post     加工结果：替换内容、附加上下文、截断超长输出

约定：**工具执行失败/被拒都不是异常，而是一种 ToolResult**（铁律 #7：
错误是给模型的输入）。`execute()` 把结果压成 dict 交给 AgentLoop 回填。

对应 dsh：`docs/tool-execution-pipeline.md`（pre/guard/execute/post 的
waterfall 中间件链）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

from tool_pipeline.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest
from tool_pipeline.registry import RegisteredTool, ToolDefinition, ToolRegistry

# 工具结果状态。ok=成功；其余都是"没成功"的不同原因，各自有不同的可读消息。
ToolStatus = Literal["ok", "error", "denied", "timeout", "unknown_tool"]

# pre 阶段的三种决策。
PreOutcome = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class ToolCallContext:
    """一次工具调用的上下文（贯穿三段）。arguments 是当前（可能已被改写）的参数。"""

    tool_name: str
    arguments: dict[str, Any]
    definition: ToolDefinition
    scope: str


@dataclass(frozen=True)
class PreDecision:
    """一个中间件在 pre 阶段给出的决策。

    - outcome=allow/deny/ask
    - reason：deny/ask 时说明原因（拒绝原因会回填给模型）
    - arguments：非 None 时替换当前参数（前一个中间件改写后，后一个看到的是新值）
    """

    outcome: PreOutcome
    reason: str = ""
    arguments: dict[str, Any] | None = None


@dataclass
class ToolResult:
    """一次工具执行的完整结果。

    content 是交给模型的字典（成功是工具返回值；失败/拒绝是 {"error": ...}）；
    status / message / duration_ms 仅供 harness 内部与观察者使用，不进模型上下文。
    """

    status: ToolStatus
    content: dict[str, Any]
    message: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@runtime_checkable
class ToolMiddleware(Protocol):
    """中间件协议：pre/post 都是可选实现（有则调用，没有就跳过）。"""

    def pre(self, ctx: ToolCallContext) -> PreDecision | None:
        ...

    def post(self, ctx: ToolCallContext, result: ToolResult) -> ToolResult | None:
        ...


class ToolPipeline:
    """把注册表 + 审批策略 + 中间件链 + 超时拼成一条执行管线。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        approval: ApprovalPolicy | None = None,
        timeout: float | None = 30.0,
        middlewares: Sequence[ToolMiddleware] = (),
    ) -> None:
        self._registry = registry
        self._approval = approval
        self._timeout = timeout
        self._middlewares = list(middlewares)

    # ---- 供 AgentLoop 使用的适配器 ----

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """AgentLoop 的执行器契约：只返回给模型的字典。"""
        return self.run(tool_name, arguments).content

    # ---- 主管线 ----

    def run(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        resolved = self._registry.resolve(tool_name)
        if resolved is None:
            return ToolResult(
                "unknown_tool", {"error": f"未知工具：{tool_name}"}, f"未注册的工具 {tool_name}"
            )

        args = dict(arguments)
        definition = resolved.definition
        ctx = ToolCallContext(tool_name, args, definition, resolved.scope)

        # ---- pre：决策 + 参数改写 ----
        decision = self._run_pre(ctx)
        if decision.outcome == "deny":
            return self._denied(tool_name, decision.reason)
        if decision.outcome == "ask":
            verdict = self._ask(tool_name, ctx.arguments, decision.reason)
            if not verdict.allowed:
                return self._denied(tool_name, verdict.reason)
        # allow（或 ask 被批准）：用最终参数构造执行上下文
        exec_ctx = ToolCallContext(tool_name, dict(decision.arguments or args), definition, resolved.scope)

        # ---- execute：真正执行，含超时 ----
        result = self._execute(exec_ctx, resolved)

        # ---- post：加工结果 ----
        return self._run_post(exec_ctx, result)

    # ---- 三段实现 ----

    def _run_pre(self, ctx: ToolCallContext) -> PreDecision:
        """按序跑 pre 中间件：deny/ask 立即短路；参数改写累积传递。

        若没有中间件给出结论，且工具被标记 needs_approval，则自动转 ask。
        """
        current = ctx
        for middleware in self._middlewares:
            pre = getattr(middleware, "pre", None)
            if pre is None:
                continue
            decision = pre(current)
            if decision is None:
                continue
            if decision.arguments is not None:
                # 改写参数后，后续中间件与最终执行都用新参数
                current = ToolCallContext(
                    ctx.tool_name, dict(decision.arguments), ctx.definition, ctx.scope
                )
            if decision.outcome in ("deny", "ask"):
                return decision

        if ctx.definition.needs_approval:
            return PreDecision("ask", reason="该工具被标记为需要人工审批")
        return PreDecision("allow", arguments=current.arguments)

    def _ask(self, tool_name: str, arguments: dict[str, Any], reason: str) -> ApprovalDecision:
        if self._approval is None:
            # 没有配置审批策略 = 无人可问 → fail-closed
            return ApprovalDecision(False, "未配置审批策略，按 fail-closed 拒绝")
        return self._approval.approve(
            ApprovalRequest(tool_name=tool_name, arguments=dict(arguments), reason=reason)
        )

    def _execute(self, ctx: ToolCallContext, resolved: RegisteredTool) -> ToolResult:
        """执行工具函数。异常转结构化错误；超时返回 timeout（工具线程可能仍在后台）。"""
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = resolved.func(**ctx.arguments)
            except Exception as exc:  # noqa: BLE001 —— 兜底：工具炸了不能炸循环
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        start = time.perf_counter()
        thread.start()
        thread.join(self._timeout)
        duration_ms = (time.perf_counter() - start) * 1000.0

        if thread.is_alive():
            return ToolResult(
                "timeout",
                {"error": f"工具执行超时（>{self._timeout}s）", "code": "TOOL_TIMEOUT"},
                f"{ctx.tool_name} 超时",
                duration_ms,
            )
        if "error" in box:
            return ToolResult(
                "error",
                {"error": f"工具执行失败：{box['error']}"},
                str(box["error"]),
                duration_ms,
            )
        return ToolResult("ok", box["result"], duration_ms=duration_ms)

    def _run_post(self, ctx: ToolCallContext, result: ToolResult) -> ToolResult:
        """按序跑 post 中间件：每个可返回替换结果，最后一个生效。"""
        current = result
        for middleware in self._middlewares:
            post = getattr(middleware, "post", None)
            if post is None:
                continue
            replaced = post(ctx, current)
            if replaced is not None:
                current = replaced
        return current

    @staticmethod
    def _denied(tool_name: str, reason: str) -> ToolResult:
        return ToolResult(
            "denied",
            {"error": reason or "工具调用被拒绝", "denied": True},
            reason or "被拒绝",
        )
