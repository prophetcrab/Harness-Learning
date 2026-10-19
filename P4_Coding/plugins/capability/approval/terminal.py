"""approval-terminal —— 终端审批通道（能力插件）。

工具与文件策略都通过 `approval` 服务问审批（`ask(request) -> bool`）。本插件
把它接到终端：

    policy="ask"    交互式 y/n（非 TTY 时**拒绝**——fail-closed）
    policy="allow"  全部放行（`app.py --yes`）
    policy="deny"   全部拒绝（离线 / 无人值守）

**为什么单独一个插件**：终端只是审批通道的一种实现。同一个 `approval` 服务
将来可以由网页卡片（浏览器审批）顶替，工具与 fs 策略一行不用改。
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from core import Context

#: 审批服务的名字（tools-admin / fs-sandbox 都按它现取）。
APPROVAL_SERVICE = "approval"

ApprovalPolicyMode = Literal["ask", "allow", "deny"]


class TerminalApproval:
    """在终端问 y/n；非交互环境按 fail-closed 拒绝。"""

    def __init__(self, policy: ApprovalPolicyMode = "ask") -> None:
        if policy not in ("ask", "allow", "deny"):
            raise ValueError(f"审批策略不合法：{policy!r}（ask / allow / deny）")
        self._policy = policy
        #: 最近一次问过的内容（测试 / 管理面可查）。
        self.asked: list[str] = []

    def ask(self, request: Any) -> bool:
        if self._policy == "allow":
            return True
        if self._policy == "deny":
            return False
        if not sys.stdin.isatty():
            # 没有人在终端前面：不能问，就拒绝（不静默放行）。
            return False
        self.asked.append(getattr(request, "tool", None) or getattr(request, "path", "?"))
        print(f"\n[需要审批] {describe(request)}", file=sys.stderr)
        try:
            answer = input("允许吗？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes", "是")


def describe(request: Any) -> str:
    """把一次审批请求说成人话（工具名 / 路径 / 参数）。"""
    tool = getattr(request, "tool", None)
    if tool is not None:
        arguments = getattr(request, "arguments", {})
        return f"工具 {tool} 参数 {dict(arguments)!r}"
    operation = getattr(request, "operation", "?")
    path = getattr(request, "path", "?")
    return f"文件操作 {operation} {path}"


class ApprovalTerminalPlugin:
    """提供 `approval` 服务（终端 y/n）。"""

    name = "approval-terminal"

    def __init__(self, *, policy: ApprovalPolicyMode = "ask") -> None:
        self._policy = policy

    def setup(self, ctx: Context) -> None:
        ctx.provide(APPROVAL_SERVICE, TerminalApproval(self._policy))


def approval_terminal_plugin(**config: Any) -> ApprovalTerminalPlugin:
    """工厂：装配单里 `config.policy` 给 ask / allow / deny。"""
    return ApprovalTerminalPlugin(**config)


__all__ = [
    "APPROVAL_SERVICE",
    "ApprovalPolicyMode",
    "ApprovalTerminalPlugin",
    "TerminalApproval",
    "approval_terminal_plugin",
    "describe",
]
