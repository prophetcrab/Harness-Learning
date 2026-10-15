"""审批闸门 —— 需要审批的"自我操作"类工具的公共那一段。

**要解决的问题**：有一类工具会**改变 harness 自己**（装卸插件、装卸工具、
引入新代码）。它们的安全边界只有一条：**人工审批**。写这类工具时有两处会
反复踩到的坑，本模块就是为了让它们只踩一次：

**坑一：管线已经问过，服务层再问一遍 = 一次操作弹两张卡片。**
工具标了 `needs_approval=True`，管线在 pre 阶段就会问审批策略；而服务层的公共
方法**也可能被直接调用**（测试、人面板、未来别的入口），所以服务层自己也得问。
两处都问，人就看见两张一样的卡片。

对策：工具入口先 `preapproved(...)` 打上"管线已放行"的标记，服务层用
`is_preapproved(...)` 查——查到了就跳过第二次询问。标记放**线程局部**而不是
全局：工具在管线的**工作线程**里跑，全局标记会在并发调用时串味。

**坑二：没有审批策略时要**拒绝**，而不是"没人管就随便放行"。**
fail-closed 是这类操作的既定语义（`_01` 起就是），所以 `source_approval()` 在
取不到策略时直接返回"被拒"，理由是人话。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from components.tools.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

#: 线程局部的"本线程正在执行哪个已放行的工具"标记。
_state = threading.local()


@contextmanager
def preapproved(tool_name: str) -> Iterator[None]:
    """标记"当前线程正在跑这个工具的管线路径"（管线已问过审批）。"""
    previous = getattr(_state, "tool", None)
    _state.tool = tool_name
    try:
        yield
    finally:
        _state.tool = previous


def is_preapproved(tool_name: str) -> bool:
    """当前线程是否正跑在这个工具的管线路径上。"""
    return getattr(_state, "tool", None) == tool_name


def source_approval(
    policy: ApprovalPolicy | None,
    *,
    tool_name: str,
    path: str,
    source: str,
    reason: str,
) -> ApprovalDecision:
    """就"引入这段源码"问一次审批；没有策略 → 拒绝（fail-closed）。

    审批请求里带上**源码全文**：审批人要看着代码点头，这是这条路径唯一的边界。
    """
    if policy is None:
        return ApprovalDecision(
            False, "没有审批策略：引入新代码必须有人批准（fail-closed 拒绝）"
        )
    request = ApprovalRequest(
        tool_name=tool_name,
        arguments={"path": path, "bytes": len(source.encode("utf-8")), "source": source},
        reason=reason,
    )
    return policy.approve(request)


def action_approval(
    policy: ApprovalPolicy | None,
    *,
    tool_name: str,
    action: str,
    target: str,
    detail: str,
) -> ApprovalDecision:
    """就"模型要改 harness 自己"问一次审批（装卸工具/插件这类动作）。"""
    if policy is None:
        return ApprovalDecision(
            False, "没有审批策略：改动工具面必须有人批准（fail-closed 拒绝）"
        )
    request = ApprovalRequest(
        tool_name=tool_name,
        arguments={"action": action, "target": target},
        reason=detail,
    )
    return policy.approve(request)


__all__ = ["action_approval", "is_preapproved", "preapproved", "source_approval"]
