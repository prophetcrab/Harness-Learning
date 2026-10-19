"""fs —— 文件服务（基础插件）：真实读写 + 策略插座。

所有文件操作都经过 `workspace.resolve()`（边界）与 `fs/approve`（策略）：

    fs.read_text(path, session_id=...)    读；成功/失败都发 fs/read / fs/read-missing
    fs.write_text(path, text, ...)        写；先过审批 + 读后写策略
    fs.edit_text(path, old, new, ...)     替换；同样先过策略
    fs.list_dir(path=".", ...)            列目录
    fs.stat(path)                         大小 / 是否存在

**两层策略各归各的插件**：

    fs-sandbox              按模式决定要不要问（读 / 写 / 风险 / 全放行）
    fs-observation-policy   已存在的文件**必须先读过**才能改

两个策略都挂在 `fs/approve` waterfall 上，拒绝时抛 `FsDenied`——
工具层把它变成给模型看的错误，不会炸掉整个 turn。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from core import Context

#: 文件服务的名字。
FS_SERVICE = "fs"
#: 依赖的服务名（字符串；插件之间不 import，见决策 0053）。
WORKSPACE_SERVICE = "workspace"

#: 策略插座：所有读写前都会经过它（handler 拒绝 → 抛 FsDenied）。
FS_APPROVE = "fs/approve"
#: 观测事件（读后写策略靠它记账）。
FS_READ = "fs/read"
FS_READ_MISSING = "fs/read-missing"
FS_WRITE = "fs/write"


class FsError(RuntimeError):
    """文件操作失败（路径不存在、编码错误等）。"""

    code = "FS_ERROR"


class FsNotFound(FsError):
    code = "FS_NOT_FOUND"


class FileSystemService:
    """工作区内的文件读写（唯一强制点是 `Workspace.resolve`）。"""

    def __init__(self, ctx: Context, workspace: Any) -> None:
        self._ctx = ctx
        self._workspace = workspace

    @property
    def workspace(self) -> Any:
        return self._workspace

    # ------------------------------------------------------------------
    # 策略
    # ------------------------------------------------------------------

    def _approve(self, operation: str, path: Path, session_id: str | None, *, exists: bool) -> None:
        request = ApprovalRequest(
            operation=operation,  # type: ignore[arg-type]
            path=str(path),
            session_id=session_id,
            exists=exists,
            risky=False,
        )
        self._ctx.waterfall(FS_APPROVE, request, request=request)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def read_text(
        self, path: str | Path, *, session_id: str | None = None, limit: int | None = None
    ) -> str:
        target = self._workspace.resolve(path)
        exists = target.is_file()
        self._approve("read", target, session_id, exists=exists)
        if not exists:
            self._ctx.emit(FS_READ_MISSING, session_id=session_id, path=str(target))
            raise FsNotFound(f"文件不存在：{path!r}（解析为 {target}）")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise FsError(
                f"文件不是 UTF-8 文本：{path!r}（{exc}）"
            ) from exc
        self._ctx.emit(FS_READ, session_id=session_id, path=str(target), exists=True)
        if limit is not None and limit > 0:
            lines = text.splitlines(keepends=True)
            text = "".join(lines[:limit])
        return text

    # ------------------------------------------------------------------
    # 写 / 编辑
    # ------------------------------------------------------------------

    def write_text(
        self, path: str | Path, text: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        target = self._workspace.resolve(path)
        exists = target.exists()
        self._approve("write", target, session_id, exists=exists)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self._ctx.emit(FS_WRITE, session_id=session_id, path=str(target), bytes=len(text))
        return {"path": str(target), "bytes": len(text.encode("utf-8")), "created": not exists}

    def edit_text(
        self,
        path: str | Path,
        old: str,
        new: str,
        *,
        count: int = 1,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if not old:
            raise FsError("要替换的旧文本不能为空")
        target = self._workspace.resolve(path)
        exists = target.is_file()
        self._approve("edit", target, session_id, exists=exists)
        if not exists:
            raise FsNotFound(f"文件不存在：{path!r}")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            raise FsError(f"没找到要替换的文本（{old[:40]!r}…）")
        actual = occurrences if count <= 0 else min(count, occurrences)
        updated = text.replace(old, new, actual)
        target.write_text(updated, encoding="utf-8")
        self._ctx.emit(
            FS_WRITE, session_id=session_id, path=str(target), bytes=len(updated)
        )
        return {"path": str(target), "replacements": actual, "occurrences": occurrences}

    # ------------------------------------------------------------------
    # 列目录 / 元信息
    # ------------------------------------------------------------------

    def list_dir(
        self,
        path: str | Path = ".",
        *,
        session_id: str | None = None,
        recursive: bool = False,
    ) -> list[dict[str, Any]]:
        target = self._workspace.resolve(path)
        if not target.is_dir():
            raise FsError(f"不是目录：{path!r}")
        self._approve("list", target, session_id, exists=True)
        entries: list[dict[str, Any]] = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for item in sorted(iterator):
            relative = item.relative_to(self._workspace.root).as_posix()
            is_dir = item.is_dir()
            entries.append(
                {
                    "path": relative,
                    "type": "dir" if is_dir else "file",
                    "bytes": 0 if is_dir else item.stat().st_size,
                }
            )
        return entries

    def stat(self, path: str | Path) -> dict[str, Any]:
        target = self._workspace.resolve(path)
        exists = target.exists()
        return {
            "path": str(target),
            "exists": exists,
            "type": ("dir" if target.is_dir() else "file") if exists else None,
            "bytes": target.stat().st_size if exists and target.is_file() else 0,
        }

    def snapshot(self) -> dict[str, Any]:
        return {"root": str(self._workspace.root)}


class FileSystemPlugin:
    """提供 `fs` 服务。装配单里应声明 `role="base"`。"""

    name = "fs"
    inject = (WORKSPACE_SERVICE,)

    def setup(self, ctx: Context) -> None:
        workspace: Any = ctx.require(WORKSPACE_SERVICE)
        ctx.provide(FS_SERVICE, FileSystemService(ctx, workspace))


def fs_plugin(**config: Any) -> FileSystemPlugin:
    """工厂（当前无配置——工作区由 workspace 插件给）。"""
    return FileSystemPlugin(**config)


__all__ = [
    "ApprovalPolicy",
    "ApprovalRequest",
    "AutoApprove",
    "AutoDeny",
    "FS_APPROVE",
    "FS_READ",
    "FS_READ_MISSING",
    "FS_SERVICE",
    "FS_WRITE",
    "FileSystemPlugin",
    "FileSystemService",
    "FsDenied",
    "FsError",
    "FsNotFound",
    "MODES",
    "ObservationTracker",
    "Operation",
    "RiskJudge",
    "SandboxMode",
    "SandboxPolicy",
    "default_risk_judge",
    "fs_plugin",
]


# ======================================================================
# 以下合并自 components/fs/sandbox.py
# ======================================================================


#: 沙箱模式。
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


# ======================================================================
# 以下合并自 components/fs/observation.py
# ======================================================================


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
