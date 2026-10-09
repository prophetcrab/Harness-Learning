"""LocalFS —— FileSystem 接缝的本地磁盘实现（Provider 角色）。

把路径解析到某个根目录下的真实文件。它就是"普通主机文件"：只提供机制
（怎么写盘、怎么读回），**不做策略**——路径是否允许、写去哪要不要拦，
是上层（工具 / 策略型 provider）的事。

对应 dsh：`packages/fs/fs-local`（backend 只负责"跨执行环境一致的文件操作"，
沙箱限制由 `fs-sandbox` 另行提供）。
"""

from __future__ import annotations

from pathlib import Path

from providers.filesystem import (
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_NOT_A_DIR,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    FsError,
)


class LocalFS:
    """以 root 为根的真实磁盘文件系统（FileSystem 协议实现）。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        """根目录（绝对路径，只读）。"""
        return self._root

    # ------------------------------------------------------------------
    # 内部：路径解析
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """把相对路径解析为真实路径（root / path）。"""
        return (self._root / path).resolve()

    # ------------------------------------------------------------------
    # FileSystem 协议
    # ------------------------------------------------------------------

    def read_text(self, path: str) -> str:
        target = self._resolve(path)
        if not target.exists():
            raise FsError(FS_NOT_FOUND, f"文件不存在：{path}")
        if target.is_dir():
            raise FsError(FS_NOT_A_FILE, f"这是一个目录，不是文件：{path}")
        return target.read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        target = self._resolve(path)
        if target.is_dir():
            raise FsError(FS_NOT_A_FILE, f"这是一个目录，不是文件：{path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def edit_text(self, path: str, old: str, new: str) -> None:
        text = self.read_text(path)  # 不存在/是目录的错误在这里统一抛出
        count = text.count(old)
        if count == 0:
            raise FsError(FS_EDIT_NO_MATCH, f"待替换的文本未找到：{old!r}")
        if count > 1:
            raise FsError(
                FS_EDIT_AMBIGUOUS,
                f"待替换的文本出现 {count} 次，拒绝猜测替换哪一处：{old!r}",
            )
        self.write_text(path, text.replace(old, new, 1))

    def list_files(self, path: str = ".") -> list[str]:
        target = self._resolve(path)
        if not target.exists():
            raise FsError(FS_NOT_FOUND, f"目录不存在：{path}")
        if not target.is_dir():
            raise FsError(FS_NOT_A_DIR, f"这不是一个目录：{path}")
        return sorted(
            p.relative_to(self._root).as_posix() for p in target.rglob("*") if p.is_file()
        )


__all__ = ["LocalFS"]
