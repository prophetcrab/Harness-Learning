"""fs-sandbox —— 真实读写隔离沙箱（能力插件）：四种审批模式。

| 模式 | 读 | 写 / 编辑 |
|---|---|---|
| `all`（全部审核） | 要审批 | 要审批 |
| `changes`（变更确认） | 放行 | 要审批 |
| `risky`（自主确认） | 放行 | 有风险时审批；同会话批准过的相同请求不再问 |
| `free`（完全访问） | 放行 | 放行（工作区外的操作仍被 workspace 拦住） |

它挂在 `fs/approve` 上——**不修改工具、不修改 fs 服务**：换模式只是换这个
插件的配置，或者干脆不装它（那样就回到 fs 的默认行为：没有策略 = 不额外审批，
但工作区边界永远在）。

审批通道优先用装配方注入的 policy，其次用 `approval` 服务；都没有 →
**fail-closed**（拒绝）。

**插件边界**（决策 0053）：本文件不认识 `fs` 插件，只认服务名 `"fs"`、事件名
`"fs/approve"` 与 `ApprovalRequest` 的**数据形状**（鸭子类型）。下面的
`ApprovalRequest` / `SandboxPolicy` 是本地副本——刻意与其他插件各存一份。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from core import Context

# --- 服务/事件名（字符串；插件之间只靠这些约定协作）---
FS_SERVICE = "fs"
FS_APPROVE = "fs/approve"
#: 沙箱服务的名字（管理面读 mode / approved 数）。
FS_SANDBOX_SERVICE = "fs-sandbox"


# ---------------------------------------------------------------------------
# 本地副本：审批请求与沙箱策略（来自 plugins/base/fs.py）
# ---------------------------------------------------------------------------

SandboxMode = Literal["all", "changes", "risky", "free"]
MODES: tuple[SandboxMode, ...] = ("all", "changes", "risky", "free")

#: 操作种类。
Operation = Literal["read", "write", "edit", "list"]


class FsDenied(RuntimeError):
    """文件操作被策略拒绝（fail loud：工具层把它变成给模型的解释）。"""

    code = "FS_DENIED"


@dataclass(frozen=True)
class ApprovalRequest:
    """一次审批请求（给审批人看的全部信息）。"""

    operation: Operation
    path: str
    session_id: str | None
    exists: bool
    risky: bool
    reason: str = ""


class ApprovalPolicy(Protocol):
    """审批通道：返回 True = 允许，False = 拒绝。"""

    def ask(self, request: ApprovalRequest) -> bool:
        ...


class AutoApprove:
    def ask(self, request: ApprovalRequest) -> bool:
        return True


class AutoDeny:
    def ask(self, request: ApprovalRequest) -> bool:
        return False


#: 风险判定：默认"覆盖已存在的文件"算有风险；调用方可以换。
RiskJudge = Callable[[ApprovalRequest], bool]


def default_risk_judge(request: ApprovalRequest) -> bool:
    """默认风险规则（保守但不过度打扰）。"""
    if request.operation in ("write", "edit"):
        return request.exists
    return False


class SandboxPolicy:
    """按模式决定"要不要问"，并按会话去重（`risky` 模式）。"""

    def __init__(
        self,
        mode: SandboxMode = "changes",
        *,
        approval: ApprovalPolicy | None = None,
        risk_judge: RiskJudge | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"沙箱模式不合法：{mode!r}（可用：{'、'.join(MODES)}）")
        self._mode = mode
        self._approval = approval or AutoDeny()  # 没装审批通道 → fail-closed
        self._risk_judge = risk_judge or default_risk_judge
        #: 已批准过的 (session, operation, path)——`risky` 模式下不再重复问。
        self._approved: set[tuple[str, str, str]] = set()

    @property
    def mode(self) -> SandboxMode:
        return self._mode

    def requires_approval(self, request: ApprovalRequest) -> bool:
        if self._mode == "free":
            return False
        if self._mode == "all":
            return True
        if self._mode == "changes":
            return request.operation in ("write", "edit")
        # risky：只在（本地判定）有风险时问，且同一会话同一请求只问一次。
        if not self._risk_judge(request):
            return False
        key = (request.session_id or "", request.operation, request.path)
        return key not in self._approved

    def approve(self, request: ApprovalRequest) -> None:
        """需要审批时走审批通道；拒绝 → FsDenied。"""
        if not self.requires_approval(request):
            return
        if not self._approval.ask(request):
            raise FsDenied(
                f"操作被拒绝（沙箱模式 {self._mode}）：{request.operation} {request.path}"
                + (f"（{request.reason}）" if request.reason else "")
            )
        self._approved.add((request.session_id or "", request.operation, request.path))

    def reset_session(self, session_id: str) -> None:
        """清掉某个会话的"已批准"记录（例如用户主动重置）。"""
        self._approved = {item for item in self._approved if item[0] != session_id}

    def snapshot(self) -> dict[str, object]:
        return {"mode": self._mode, "approved": len(self._approved)}


class _LazyApproval:
    """每次 `ask()` 现取 `approval` 服务；没有就 fail-closed。

    插件装载顺序无关（0042）：审批通道可能排在文件策略之后。
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def ask(self, request: Any) -> bool:
        service = self._ctx.get("approval")
        if service is None or not hasattr(service, "ask"):
            return False
        return bool(service.ask(request))


class FsSandboxPlugin:
    """按模式拦截文件操作。"""

    name = "fs-sandbox"
    inject = (FS_SERVICE,)

    def __init__(
        self,
        *,
        mode: SandboxMode = "changes",
        approval: ApprovalPolicy | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"沙箱模式不合法：{mode!r}（可用：{'、'.join(MODES)}）")
        self._mode = mode
        self._approval = approval

    def setup(self, ctx: Context):
        policy = SandboxPolicy(
            self._mode,
            # 用时现取（理由同 tools-admin）：审批通道可能在本插件之后装载。
            approval=self._approval or _LazyApproval(ctx),
        )
        ctx.provide(FS_SANDBOX_SERVICE, policy)

        def on_approve(value: Any, request: ApprovalRequest, **_: Any) -> Any:
            policy.approve(request)
            return value

        # 订阅随插件回卷：卸载沙箱，审批策略立刻消失。
        ctx.on(FS_APPROVE, on_approve)


def fs_sandbox_plugin(**config: Any) -> FsSandboxPlugin:
    """工厂：装配单的 `config` 可给 `mode` / `approval`。"""
    return FsSandboxPlugin(**config)


__all__ = ["FS_SANDBOX_SERVICE", "FsSandboxPlugin", "fs_sandbox_plugin"]
