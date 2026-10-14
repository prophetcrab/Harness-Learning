"""web-approval 插件 —— 把"问人"搬到浏览器里：手动审批（P3 `_01` 的配套机制）。

原来的审批策略只有两种落点：终端 y/n（`PromptApprover`）和自动放行/拒绝。
界面场景两者都不合适——没有终端可问、自动放行又等于没审。本插件提供第三种：

    WebApprovalPolicy —— ApprovalPolicy 的实现：
      1. 请求推给浏览器（`__approval` 帧：工具名 / 参数 / 原因 / 可选代码预览）；
      2. 阻塞等用户点"批准 / 拒绝"（HTTP `POST /api/approvals/<id>`）；
      3. **问不到人一律 fail-closed**：没有连接、超时（默认 300 秒）都按拒绝。

谁在用它：`AgentService` 在会话打开时解析 `approval` 服务（见 plugins/agent.py）——
装配方没有显式给策略时，就用这里提供的这一个。于是 `chat.py --web` 默认就是
"浏览器里点批准"，而不是"没有终端所以全部拒绝"。

这是"安装新插件"这条路的最后一环：模型写代码 → 点 `install_plugin(path)` →
**你在浏览器里看到代码并决定批不批** → 批准才执行、装载、出现在左侧台账。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from components.tools import ApprovalDecision, ApprovalRequest
from kernel import Context
from plugins.events import APPROVAL_REQUESTED, APPROVAL_RESOLVED
from plugins.services import APPROVAL_SERVICE, FS_SERVICE

#: 推送通道：webui 在跑 turn 时把自己的写帧函数挂上来（kind, payload）。
Sender = Callable[[str, dict[str, Any]], None]
#: 预览构造器：给定工具名与参数，返回要展示给审批人的文本（或 None）。
PreviewBuilder = Callable[[str, dict[str, Any]], "str | None"]


@dataclass
class PendingApproval:
    """一次等待人工决定的审批请求。"""

    id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    preview: str | None = None
    event: threading.Event = field(default_factory=threading.Event)
    decision: ApprovalDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool_name,
            "arguments": self.arguments,
            "reason": self.reason,
            "preview": self.preview,
        }


class WebApprovalPolicy:
    """浏览器里的审批：推帧 → 等点击 → 无应答 fail-closed。"""

    def __init__(self, *, timeout: float = 300.0, preview: PreviewBuilder | None = None) -> None:
        self._timeout = timeout
        self._preview = preview
        self._sender: Sender | None = None
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()
        self._ctx: Context | None = None

    # ------------------------------------------------------------------
    # 与界面（webui）的接缝
    # ------------------------------------------------------------------

    def bind_ctx(self, ctx: Context) -> None:
        """插件装载时注入（发 approval/* 事件用）。"""
        self._ctx = ctx

    def attach_stream(self, sender: Sender) -> Sender:
        """跑 turn 前挂上当前连接的写帧函数；**返回句柄**（卸载时凭它校验）。

        为什么要返回句柄：turn 是串行的，但"等待审批"会让一个 turn 长时间挂着，
        期间另一个 turn 可能已经挂上自己的连接。如果卸载时不校验身份，
        先结束的那个 turn 会把**后来者**的通道一起清掉——审批就会误报
        "浏览器未连接"（实测踩到过）。所以卸载只清"还是我挂的那个"。
        """
        with self._lock:
            self._sender = sender
        return sender

    def detach_stream(self, sender: Sender | None = None) -> None:
        """摘掉推流通道；给了句柄就只摘自己挂的那个（见 attach_stream）。"""
        with self._lock:
            if sender is None or self._sender is sender:
                self._sender = None

    @property
    def timeout(self) -> float:
        return self._timeout

    def pending_list(self) -> list[dict[str, Any]]:
        """当前等待中的审批（页面刷新后也能补看到）。"""
        with self._lock:
            return [p.to_dict() for p in self._pending.values()]

    def resolve(self, request_id: str, approved: bool, note: str = "") -> bool:
        """用户点了批准/拒绝；返回是否命中了一个等待中的请求。"""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.decision = ApprovalDecision(
            bool(approved),
            note or ("用户在浏览器批准" if approved else "用户在浏览器拒绝"),
        )
        pending.event.set()
        return True

    # ------------------------------------------------------------------
    # ApprovalPolicy 协议
    # ------------------------------------------------------------------

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        """推一次审批到浏览器并等待；没人应答按 fail-closed 拒绝。"""
        with self._lock:
            sender = self._sender
        if sender is None:
            return ApprovalDecision(
                False, "浏览器未连接：无人可审批，按 fail-closed 拒绝"
            )

        preview = None
        if self._preview is not None:
            try:
                preview = self._preview(request.tool_name, dict(request.arguments))
            except Exception:  # noqa: BLE001 —— 预览失败不影响审批本身
                preview = None

        pending = PendingApproval(
            id=f"apv-{uuid.uuid4().hex[:8]}",
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            reason=request.reason,
            preview=preview,
        )
        with self._lock:
            self._pending[pending.id] = pending
        self._emit(APPROVAL_REQUESTED, pending=pending.to_dict())

        try:
            sender("__approval", pending.to_dict())
        except Exception:  # noqa: BLE001 —— 推不出去就别等了，直接拒绝
            with self._lock:
                self._pending.pop(pending.id, None)
            return ApprovalDecision(False, "审批请求推送失败（连接已断），按 fail-closed 拒绝")

        answered = pending.event.wait(self._timeout)
        with self._lock:
            self._pending.pop(pending.id, None)
        if not answered:
            self._emit(
                APPROVAL_RESOLVED, id=pending.id, tool=pending.tool_name,
                approved=False, note="timeout",
            )
            return ApprovalDecision(
                False, f"审批超时（{self._timeout:g} 秒无人应答），按 fail-closed 拒绝"
            )
        decision = pending.decision or ApprovalDecision(False, "无决定，按 fail-closed 拒绝")
        self._emit(
            APPROVAL_RESOLVED, id=pending.id, tool=pending.tool_name,
            approved=decision.allowed, note=decision.reason,
        )
        return decision

    # ------------------------------------------------------------------

    def _emit(self, event: str, **payload: Any) -> None:
        if self._ctx is not None:
            self._ctx.emit(event, **payload)


def _build_preview(ctx: Context) -> PreviewBuilder:
    """默认预览：安装/写入类操作把文件内容摆给审批人看。"""
    fs = ctx.get(FS_SERVICE)

    def preview(tool_name: str, arguments: dict[str, Any]) -> str | None:
        if fs is None:
            return None
        if tool_name not in ("install_plugin", "write_file", "edit_file"):
            return None
        path = str(arguments.get("path") or "")
        if tool_name == "install_plugin":
            try:
                return fs.read_text(path)[:2000]
            except Exception:  # noqa: BLE001 —— 读不到就不给预览
                return None
        return None

    return preview


class WebApprovalPlugin:
    """提供 `approval` 服务（浏览器审批策略）；界面插件会自动接管它的推流。"""

    name = "web-approval"

    def __init__(self, *, timeout: float = 300.0) -> None:
        self._timeout = timeout

    def setup(self, ctx: Context) -> None:
        policy = WebApprovalPolicy(timeout=self._timeout, preview=_build_preview(ctx))
        policy.bind_ctx(ctx)
        ctx.provide(APPROVAL_SERVICE, policy)


def web_approval_plugin(*, timeout: float = 300.0) -> WebApprovalPlugin:
    """工厂：`timeout` 是"等用户点击"的秒数，超时按拒绝处理。"""
    return WebApprovalPlugin(timeout=timeout)


__all__ = [
    "PendingApproval",
    "WebApprovalPlugin",
    "WebApprovalPolicy",
    "web_approval_plugin",
]
