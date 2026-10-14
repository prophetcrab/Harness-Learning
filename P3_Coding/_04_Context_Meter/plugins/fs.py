"""fs 插件 —— 提供文件系统能力（`fs` 服务）。

三种实现，同一个协议（`FileSystem`），因此工具面 / agent 完全感知不到差别：

    local_fs_plugin(root)   真实磁盘（root 之下）
    memory_fs_plugin()      内存字典（进程结束即消失；测试零副作用）
    jail_fs_plugin(root)    真实磁盘 + 工作区围栏（越界改动被拒）

插件名带实现后缀（`fs:LocalFS` / `fs:MemoryFS` / `fs:WorkspaceJailFS`），
台账与 dump 里"这一槽是谁提供的"一眼可读。
"""

from __future__ import annotations

from pathlib import Path

from components.fs import FileSystem, LocalFS, MemoryFS, WorkspaceJailFS
from kernel import Context
from plugins.services import FS_SERVICE


class FsPlugin:
    """把一个 FileSystem 实现装成 `fs` 服务（卸载自动回卷）。"""

    def __init__(self, provider: FileSystem) -> None:
        self._provider = provider
        self.name = f"fs:{type(provider).__name__}"

    def setup(self, ctx: Context) -> None:
        ctx.provide(FS_SERVICE, self._provider)


def local_fs_plugin(root: str | Path) -> FsPlugin:
    """真实磁盘（根目录按需创建）。"""
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return FsPlugin(LocalFS(path))


def memory_fs_plugin() -> FsPlugin:
    """内存文件系统：测试与演示的默认（磁盘零痕迹）。"""
    return FsPlugin(MemoryFS())


def jail_fs_plugin(root: str | Path) -> FsPlugin:
    """真实磁盘 + 围栏：越出 root 的写/改被拒（读放行——检索不越权）。"""
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return FsPlugin(WorkspaceJailFS(LocalFS(path)))


__all__ = ["FsPlugin", "jail_fs_plugin", "local_fs_plugin", "memory_fs_plugin"]
