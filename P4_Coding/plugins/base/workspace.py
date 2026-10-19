"""workspace —— 工作区（基础插件）：限定 LLM / 工具的工作范围。

一个工作区就是一个目录 + 一条边界：文件读写、命令执行都在它下面。本插件只
负责**边界本身**，不提供文件或命令能力（那是能力插件的事）：

    workspace.root                  当前工作区目录（绝对路径）
    workspace.resolve(path)         把相对路径解析成绝对路径；越界 → 拒绝
    workspace.contains(path)        某个路径是否在工作区内
    workspace.info()                目录 / 大小 / 文件数（给管理面看）

**边界规则**（fail loud）：

    · 相对路径解析到工作区根下；
    · 绝对路径必须在工作区内；
    · `..` 穿越后也要落在工作区内（解析后判断，不做字符串前缀比较）；
    · 符号链接按解析后的真实路径判断（`resolve()` 会跟随链接）。

本插件**不创建**目录、**不读写**文件内容——那是 `tool-fs` 之类的事。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core import Context

#: 工作区服务的名字。
WORKSPACE_SERVICE = "workspace"


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


class WorkspacePlugin:
    """提供 `workspace` 服务。装配单里应声明 `role="base"`。

    构造参数 `root` 决定工作区；`must_exist=True` 时目录必须已存在
    （工作区是"给人用的目录"，不该由基座默默创建）。
    """

    name = "workspace"

    def __init__(self, *, root: str | Path, must_exist: bool = True) -> None:
        self._root = root
        self._must_exist = must_exist

    def setup(self, ctx: Context) -> None:
        ctx.provide(WORKSPACE_SERVICE, Workspace(self._root, must_exist=self._must_exist))


def workspace_plugin(**config: Any) -> WorkspacePlugin:
    """工厂：装配单的 `config` 里给 `root`。"""
    return WorkspacePlugin(**config)


__all__ = [
    "WORKSPACE_SERVICE",
    "Workspace",
    "WorkspaceError",
    "WorkspacePlugin",
    "workspace_plugin",
]
