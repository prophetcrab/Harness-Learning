"""workspace-admin —— 工作区管理（能力插件）：编辑、增删、查看。

给管理面 / 模型工具用的工作区管理能力：

    list()                  所有登记的工作区（名字 → 目录 / 大小 / 文件数）
    create(name, root)      登记一个新工作区（目录必须已存在）
    remove(name)            移除登记（**不删目录**——那是破坏性操作，不在这里做）
    set_active(name)        切换当前工作区
    active()                当前工作区（与 `workspace` 服务是同一个对象）
    info(name=None)         目录 / 文件数 / 总字节 / 人类可读大小

**边界**：本插件不改 `workspace` 服务的边界语义——它只是"选哪个工作区"的
登记表；路径越界检查始终由 `Workspace` 自己做。

**不删目录**：`remove` 只摘登记。真删文件是破坏性操作，应该由人在文件系统里
做，或由一个显式审批过的工具做——这里不给一条捷径。
"""

from __future__ import annotations

import os

from typing import Any
from pathlib import Path

from core import Context



# --- 本地类型别名 ---
class WorkspaceError(RuntimeError):
    """路径越界 / 工作区不可用（fail loud）。"""

class Workspace:
    """一个工作区目录 + 边界检查。"""

    def __init__(self, root: str | Path, *, must_exist: bool = True) -> None:
        path = Path(root).expanduser()
        if must_exist:
            try:
                path = path.resolve(strict=True)
            except OSError as exc:
                raise WorkspaceError(f"工作区不存在或无法访问：{path}（{exc}）") from exc
            if not path.is_dir():
                raise WorkspaceError(f"工作区不是目录：{path}")
        else:
            path = path.absolute()
        self._root = path

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """把路径解析成工作区内的绝对路径；越界 → WorkspaceError。

        先按 `root / path` 拼（相对路径），再 `resolve()`（跟随符号链接、
        消掉 `..`），最后检查结果是否仍在 root 下。**不做字符串前缀比较**——
        `root/../outside` 这种前缀看着在界内，解析后其实在外面。
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:  # 路径不可解析（坏链接等）
            raise WorkspaceError(f"路径无法解析：{path!r}（{exc}）") from exc
        if not self.contains(resolved):
            raise WorkspaceError(
                f"路径越界：{path!r} 解析到 {resolved}，不在工作区 {self._root} 内"
            )
        return resolved

    def contains(self, path: str | Path) -> bool:
        """某个（已存在的）路径是否在工作区内。"""
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
        except OSError:
            return False
        return resolved == self._root or self._root in resolved.parents

    def relative(self, path: str | Path) -> str:
        """工作区内的相对路径（给日志 / 工具结果用，不泄漏绝对路径）。"""
        return str(self.resolve(path).relative_to(self._root)).replace(os.sep, "/")

    def info(self) -> dict[str, Any]:
        """目录 / 文件数 / 总字节数（管理面展示用；不递归进符号链接目录之外）。"""
        files = 0
        total = 0
        for current_root, _dirs, names in os.walk(self._root):
            for name in names:
                full = Path(current_root) / name
                try:
                    total += full.stat().st_size
                except OSError:
                    continue
                files += 1
        return {
            "root": str(self._root),
            "files": files,
            "bytes": total,
            "human_size": _human_size(total),
        }

    def snapshot(self) -> dict[str, Any]:
        return self.info()

    def __repr__(self) -> str:
        return f"Workspace({str(self._root)!r})"

def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# --- 服务/事件名（字符串，本地复刻）---
WORKSPACE_SERVICE = "workspace"



#: 管理服务的名字。
WORKSPACE_ADMIN_SERVICE = "workspace-admin"


class WorkspaceAdminService:
    """工作区登记表 + 当前选择。

    它**不替换** `workspace` 服务：`workspace` 永远是"当前生效的那一个"，
    本服务负责在多个候选之间切换。
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspaces: dict[str, Workspace] = {"default": workspace}
        self._active = "default"

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def names(self) -> tuple[str, ...]:
        return tuple(self._workspaces)

    def active_name(self) -> str:
        return self._active

    def active(self) -> Workspace:
        return self._workspaces[self._active]

    def info(self, name: str | None = None) -> dict[str, Any]:
        workspace = self._resolve(name)
        return {"name": self._name_of(workspace), **workspace.info()}

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "root": str(workspace.root),
                "active": name == self._active,
                **{
                    key: value
                    for key, value in workspace.info().items()
                    if key != "root"
                },
            }
            for name, workspace in self._workspaces.items()
        ]

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def create(self, name: str, root: str | Path) -> dict[str, Any]:
        """登记一个新工作区；重名 / 目录不存在都 fail loud。"""
        if not isinstance(name, str) or not name:
            raise ValueError(f"工作区名必须是非空字符串：{name!r}")
        if name in self._workspaces:
            raise ValueError(f"工作区已存在：{name!r}（不静默覆盖）")
        if not Path(root).expanduser().is_dir():
            raise ValueError(f"工作区目录不存在：{root!r}（本插件不创建目录）")
        self._workspaces[name] = Workspace(root)
        return {"name": name, "root": str(self._workspaces[name].root)}

    def remove(self, name: str) -> None:
        """移除登记（**不删目录**）；当前工作区不能移除。"""
        if name not in self._workspaces:
            raise ValueError(f"工作区不存在：{name!r}")
        if name == self._active:
            raise ValueError(
                f"{name!r} 是当前工作区，不能移除（先 set_active 别的）"
            )
        del self._workspaces[name]

    def set_active(self, name: str) -> None:
        if name not in self._workspaces:
            raise ValueError(
                f"工作区不存在：{name!r}（可用：{'、'.join(self.names())}）"
            )
        self._active = name

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve(self, name: str | None) -> Workspace:
        if name is None:
            return self.active()
        if name not in self._workspaces:
            raise ValueError(f"工作区不存在：{name!r}")
        return self._workspaces[name]

    def _name_of(self, workspace: Workspace) -> str:
        for name, item in self._workspaces.items():
            if item is workspace:
                return name
        return "<unknown>"


class WorkspaceAdminPlugin:
    """提供 `workspace-admin` 服务（登记 / 切换 / 查看）。"""

    name = "workspace-admin"
    inject = (WORKSPACE_SERVICE,)

    def setup(self, ctx: Context) -> None:
        workspace: Workspace = ctx.require(WORKSPACE_SERVICE)
        ctx.provide(WORKSPACE_ADMIN_SERVICE, WorkspaceAdminService(workspace))


def workspace_admin_plugin(**config: Any) -> WorkspaceAdminPlugin:
    """工厂（当前无配置）。"""
    return WorkspaceAdminPlugin(**config)


__all__ = [
    "WORKSPACE_ADMIN_SERVICE",
    "WorkspaceAdminPlugin",
    "WorkspaceAdminService",
    "workspace_admin_plugin",
]
