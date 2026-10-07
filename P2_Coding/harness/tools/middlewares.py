"""示例中间件 —— 演示 pre/post 两段如何各管一件事。

中间件是"关注点"，不是"工具"：同一个中间件可以作用在所有工具上。
写自己的中间件只需实现 pre / post（可只实现其一），返回 None 表示不干预。
"""

from __future__ import annotations

from collections.abc import Iterable

from harness.tools.pipeline import PreDecision, ToolCallContext, ToolResult


class DenyTools:
    """pre：按名单硬性拒绝某些工具（例如只读会话禁用写工具）。"""

    def __init__(self, names: Iterable[str], reason: str = "该工具在当前会话被禁用") -> None:
        self._names = set(names)
        self._reason = reason

    def pre(self, ctx: ToolCallContext) -> PreDecision | None:
        if ctx.tool_name in self._names:
            return PreDecision("deny", self._reason)
        return None


class RequireApproval:
    """pre：按名单把某些工具转成"需审批"（等价于定义里的 needs_approval，
    但可以在不改编定义的前提下按会话临时收紧）。"""

    def __init__(self, names: Iterable[str], reason: str = "该工具在本会话需要审批") -> None:
        self._names = set(names)
        self._reason = reason

    def pre(self, ctx: ToolCallContext) -> PreDecision | None:
        if ctx.tool_name in self._names:
            return PreDecision("ask", self._reason)
        return None


class TruncateOutput:
    """post：把结果里过长的字符串值截断，防止超长输出灌爆上下文。"""

    def __init__(self, max_chars: int = 2000) -> None:
        self._max_chars = max_chars

    def post(self, ctx: ToolCallContext, result: ToolResult) -> ToolResult | None:
        truncated = False
        new_content = {}
        for key, value in result.content.items():
            if isinstance(value, str) and len(value) > self._max_chars:
                new_content[key] = (
                    value[: self._max_chars] + f"...[已截断，原长 {len(value)} 字符]"
                )
                truncated = True
            else:
                new_content[key] = value
        if not truncated:
            return None
        new_content["truncated"] = True
        return ToolResult(result.status, new_content, result.message, result.duration_ms)
