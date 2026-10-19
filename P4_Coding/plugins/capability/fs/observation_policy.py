"""fs-observation-policy —— 修改文件前必须先读（能力插件）。

挂在 `fs/approve` 上：**已存在的文件**在写入 / 编辑前，必须在同一会话里被
读过（或确认过不存在）。新建文件不受限制。

为什么要有它：模型凭想象改文件（"盲改"）是破坏性 bug 的主要来源；读一遍
成本很低，收益是它至少看过真实内容。

"读过"的记录来自 `fs/read` / `fs/read-missing` 事件——所以任何走 fs 服务的
读取都会被记账（包括工具之外的程序化读取）。

**插件边界**（决策 0053）：本文件不认识 `fs` 插件，只认服务名、事件名与
`ApprovalRequest` 的**数据形状**（鸭子类型）。下面的 `ApprovalRequest` /
`FsDenied` / `ObservationTracker` 是本地副本——刻意与其他插件各存一份。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Literal

from core import Context

# --- 服务/事件名（字符串；插件之间只靠这些约定协作）---
FS_SERVICE = "fs"
FS_APPROVE = "fs/approve"
FS_READ = "fs/read"
FS_READ_MISSING = "fs/read-missing"
#: 策略服务的名字（管理面可以读/清）。
FS_OBSERVATION_SERVICE = "fs-observation-policy"


# ---------------------------------------------------------------------------
# 本地副本：审批请求 / 拒绝异常 / 观察账本（来自 plugins/base/fs.py）
# ---------------------------------------------------------------------------

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


class ObservationTracker:
    """按 (session, path) 记录观察结果。"""

    def __init__(self) -> None:
        #: session → path → 是否存在
        self._seen: dict[str, dict[str, bool]] = {}
        self._lock = threading.RLock()

    def record(self, session_id: str | None, path: str, *, exists: bool) -> None:
        with self._lock:
            self._seen.setdefault(session_id or "", {})[path] = exists

    def observed(self, session_id: str | None, path: str) -> bool | None:
        """返回 True（看过且存在）/ False（看过且不存在）/ None（没看过）。"""
        with self._lock:
            return self._seen.get(session_id or "", {}).get(path)

    def clear_session(self, session_id: str | None) -> None:
        with self._lock:
            self._seen.pop(session_id or "", None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {session: len(paths) for session, paths in self._seen.items()}


class FsObservationPolicyPlugin:
    """读后写策略。"""

    name = "fs-observation-policy"
    inject = (FS_SERVICE,)

    def setup(self, ctx: Context):
        tracker = ObservationTracker()
        ctx.provide(FS_OBSERVATION_SERVICE, tracker)

        def on_read(session_id: str | None, path: str, **_: Any) -> None:
            tracker.record(session_id, path, exists=True)

        def on_read_missing(session_id: str | None, path: str, **_: Any) -> None:
            tracker.record(session_id, path, exists=False)

        def on_approve(value: Any, request: ApprovalRequest, **_: Any) -> Any:
            if request.operation not in ("write", "edit") or not request.exists:
                return value
            if tracker.observed(request.session_id, request.path) is not True:
                raise FsDenied(
                    f"修改前必须先读：{request.path}（本会话还没有读过这个文件）"
                )
            return value

        ctx.on(FS_READ, on_read)
        ctx.on(FS_READ_MISSING, on_read_missing)
        ctx.on(FS_APPROVE, on_approve)


def fs_observation_policy_plugin(**config: Any) -> FsObservationPolicyPlugin:
    return FsObservationPolicyPlugin(**config)


__all__ = [
    "FS_OBSERVATION_SERVICE",
    "FsObservationPolicyPlugin",
    "fs_observation_policy_plugin",
]
