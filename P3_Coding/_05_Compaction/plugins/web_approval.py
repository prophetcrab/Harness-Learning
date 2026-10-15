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

from components.shell import DisplaySpec, RouteRequest, RouteSpec, Surface
from components.tools import ApprovalDecision, ApprovalRequest
from kernel import Context
from plugins.events import APPROVAL_REQUESTED, APPROVAL_RESOLVED
from plugins.services import APPROVAL_SERVICE, APPROVAL_SURFACE, FS_SERVICE

#: 推送通道：写一帧（kind, payload）。webui 的 turn 推流器就是它。取哪一条
#: 见 `WebApprovalPolicy._frames`：**优先本线程登记的**，兜底是 attach_stream 挂的。

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
    #: 发起这次审批的会话（刷新后补看时，界面靠它知道"该看哪条会话"）。
    session: str = ""
    event: threading.Event = field(default_factory=threading.Event)
    decision: ApprovalDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool_name,
            "arguments": self.arguments,
            "reason": self.reason,
            "preview": self.preview,
            "session": self.session,
        }


class WebApprovalPolicy:
    """浏览器里的审批：推帧 → 等点击 → 无应答 fail-closed。

    **推流通道按线程登记，不是全局一个**（`_05` 收尾改并行时重做）。
    原先是一个 `_sender` 槽：turn 开始挂上、结束摘掉。串行时代没问题（同一时刻
    只有一个 turn），但**并行之后它必然出错**——A 会话在等审批时，B 会话挂上
    自己的通道覆盖了它，A 的审批帧就推到了 B 的页面上（或者推丢，等 300 秒超时）。

    现在按**线程**登记：工具管线在哪个线程跑 turn，`approve()` 就取那个线程的
    通道（turn 的线程是固定的，从 reserve 到收尾都是同一条）。于是并行时两条
    会话各问各的人，互不干扰。

    （为什么用线程而不是 ContextVar：本项目的阶段目录各持一份同名包、测试会
    驱逐 `sys.modules`——同一个模块可能加载成两份，模块级全局对象因此"set 的
    那份与读的那份不是同一个"。挂在**服务对象实例**上的登记表绕开了这个问题，
    因为服务对象是运行时单例。）
    """

    def __init__(self, *, timeout: float = 300.0, preview: PreviewBuilder | None = None) -> None:
        self._timeout = timeout
        self._preview = preview
        #: 兜底通道：显式 `attach_stream` 挂的那个（终端/测试等"没有登记线程"的入口用）。
        self._sender: Sender | None = None
        #: 线程 id → 该线程正在跑的 turn 的写帧端（并行时每人一份）。
        self._thread_senders: dict[int, Sender] = {}
        #: 线程 id → 那条 turn 的会话 id（审批要标出"我属于哪条会话"）。
        self._thread_sessions: dict[int, str] = {}
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
        """挂一个兜底通道；**返回句柄**（摘除时凭它校验身份）。

        为什么要返回句柄：卸载时不校验身份的话，先结束的那个调用方会把
        **后来者**的通道一起清掉——审批就会误报"浏览器未连接"（实测踩到过）。
        """
        with self._lock:
            self._sender = sender
        return sender

    def detach_stream(self, sender: Sender | None = None) -> None:
        """摘掉兜底通道；给了句柄就只摘自己挂的那个（见 attach_stream）。"""
        with self._lock:
            if sender is None or self._sender is sender:
                self._sender = None

    def attach_thread_stream(self, sender: Sender, *, session: str = "") -> None:
        """给**当前线程**登记一个推流通道（执行器在跑 turn 的线程里调）。

        `session` 一并记下：审批要带上"我属于哪条会话"。没有它的话，刷新页面后
        补看到的审批卡片只能画在**当前打开的会话**里——而那个 turn 可能属于另一条
        会话，用户看到的是一张"凭空出现在别的对话里"的批准框，批准之后也看不到
        结果（那条会话没有被订阅）。见 `PendingApproval.session`。
        """
        with self._lock:
            ident = threading.get_ident()
            self._thread_senders[ident] = sender
            self._thread_sessions[ident] = session

    def detach_thread_stream(self) -> None:
        """摘掉当前线程的登记（turn 收尾时调，别让线程 id 复用撞上旧通道）。"""
        with self._lock:
            ident = threading.get_ident()
            self._thread_senders.pop(ident, None)
            self._thread_sessions.pop(ident, None)

    def _session_of_this_thread(self) -> str:
        with self._lock:
            return self._thread_sessions.get(threading.get_ident(), "")

    def _frames(self) -> Sender | None:
        """当前可用的推流通道：**先看本线程登记的，再看兜底的**。"""
        with self._lock:
            sender = self._thread_senders.get(threading.get_ident())
        return sender or self._sender

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
        sender = self._frames()
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
            session=self._session_of_this_thread(),
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


#: 审批事件的显示规则（壳按 renderer 分派）。
DISPLAY: tuple[DisplaySpec, ...] = (
    DisplaySpec("__approval", "fold", "需要你的批准：{tool}", "preview", order=1),
)


def _list_pending(request: RouteRequest) -> None:
    """GET /api/approvals —— 当前等待中的审批（页面刷新后也能补看到）。"""
    policy = request.ctx.get(APPROVAL_SERVICE)
    request.json({"pending": policy.pending_list() if policy is not None else []})


def _answer_pending(request: RouteRequest) -> None:
    """POST /api/approvals/<id> —— 用户点了批准/拒绝。"""
    request_id = request.params["id"]
    policy = request.ctx.get(APPROVAL_SERVICE)
    if policy is None:
        request.json({"error": "当前没有审批策略"}, status=404)
        return
    body = request.body
    approved = bool(body.get("approved"))
    if policy.resolve(request_id, approved, str(body.get("note") or "")):
        request.json({"ok": True, "id": request_id, "approved": approved})
    else:
        request.json({"error": f"没有等待中的审批：{request_id}"}, status=404)


#: 本插件的端点：补看等待中的审批、回答某一条（壳负责挂载与传输）。
APPROVAL_ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec("GET", "/api/approvals", _list_pending, order=1),
    RouteSpec("POST", "/api/approvals/<id>", _answer_pending, order=2),
)


class WebApprovalPlugin:
    """提供 `approval` 服务（浏览器审批策略）；界面插件会自动接管它的推流。"""

    name = "web-approval"
    #: 应用角色（capability）：浏览器审批——关掉退化为管线 fail-closed（仍然安全）。
    category = "capability"
    #: 可选：文件系统（用于把待写内容摆给审批人看；没有就只给参数摘要）。
    optional = (FS_SERVICE,)

    def __init__(self, *, timeout: float = 300.0) -> None:
        self._timeout = timeout

    def setup(self, ctx: Context) -> None:
        policy = WebApprovalPolicy(timeout=self._timeout, preview=_build_preview(ctx))
        policy.bind_ctx(ctx)
        ctx.provide(APPROVAL_SERVICE, policy)
        # 界面自述：审批事件怎么画 + 我的两条端点（壳负责挂载与传输）
        ctx.provide(
            APPROVAL_SURFACE, Surface(display=DISPLAY, routes=APPROVAL_ROUTES)
        )


def web_approval_plugin(*, timeout: float = 300.0) -> WebApprovalPlugin:
    """工厂：`timeout` 是"等用户点击"的秒数，超时按拒绝处理。"""
    return WebApprovalPlugin(timeout=timeout)


__all__ = [
    "PendingApproval",
    "WebApprovalPlugin",
    "WebApprovalPolicy",
    "web_approval_plugin",
]
