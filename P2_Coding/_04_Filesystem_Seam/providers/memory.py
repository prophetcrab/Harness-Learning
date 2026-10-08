"""MemoryFS —— FileSystem 接缝的内存实现（Provider 角色）。

文件只存在于一个字典里（`路径 → 文本`）：进程结束就没了，磁盘上不留一丝痕迹。
它的价值不在"功能"，而在**证明接缝是真的**：

- 同一套工具测试原样跑（不需要为 memory 写第二份）——测试从此不碰真实磁盘；
- 跑完对话后可以断言"磁盘上什么都没有"——副作用为零；
- 目录是隐式的（由文件路径推导），不需要建目录操作。

规范化约定（与 LocalFS 对齐）：路径先经 `PurePosixPath` 规范化
（折叠 `.` 与重复 `/`；`/` 分隔），返回值一律是 posix 风格相对路径。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from providers.filesystem import (
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_NOT_A_DIR,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    FsError,
)

# 根目录的规范化形态（内存命名空间的根）。
_ROOT = "."


class MemoryFS:
    """字典支撑的文件系统：写在这里的文件只活在内存里（FileSystem 协议实现）。"""

    def __init__(self) -> None:
        # 规范化路径 → 文本内容；插入顺序无意义（list_files 会排序）。
        self._files: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 内部：路径规范化与目录判定
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        """规范化路径：折叠 `.` 与重复 `/`；根统一为 "."。"""
        normalized = PurePosixPath(path).as_posix()
        return normalized if normalized else _ROOT

    @staticmethod
    def _is_under(key: str, directory: str) -> bool:
        """key 是否**位于** directory 之下（key 是文件；相等不算——那是文件本体）。"""
        if directory == _ROOT:
            return True
        return key.startswith(directory + "/")

    def _directory_exists(self, directory: str) -> bool:
        """目录存在 = 根本身，或有文件落在它下面（目录是隐式的）。"""
        if directory == _ROOT:
            return True
        return any(self._is_under(key, directory) for key in self._files)

    # ------------------------------------------------------------------
    # FileSystem 协议
    # ------------------------------------------------------------------

    def read_text(self, path: str) -> str:
        key = self._normalize(path)
        if key not in self._files:
            if self._directory_exists(key):
                raise FsError(FS_NOT_A_FILE, f"这是一个目录，不是文件：{path}")
            raise FsError(FS_NOT_FOUND, f"文件不存在：{path}")
        return self._files[key]

    def write_text(self, path: str, content: str) -> None:
        key = self._normalize(path)
        if key == _ROOT:
            raise FsError(FS_NOT_A_FILE, f"这是一个目录，不是文件：{path}")
        self._files[key] = content

    def edit_text(self, path: str, old: str, new: str) -> None:
        text = self.read_text(path)  # 不存在的错误在这里统一抛出
        count = text.count(old)
        if count == 0:
            raise FsError(FS_EDIT_NO_MATCH, f"待替换的文本未找到：{old!r}")
        if count > 1:
            raise FsError(
                FS_EDIT_AMBIGUOUS,
                f"待替换的文本出现 {count} 次，拒绝猜测替换哪一处：{old!r}",
            )
        self._files[self._normalize(path)] = text.replace(old, new, 1)

    def list_files(self, path: str = ".") -> list[str]:
        directory = self._normalize(path)
        if directory in self._files:  # 是个文件，不是目录
            raise FsError(FS_NOT_A_DIR, f"这不是一个目录：{path}")
        if not self._directory_exists(directory):
            raise FsError(FS_NOT_FOUND, f"目录不存在：{path}")
        return sorted(key for key in self._files if self._is_under(key, directory))


__all__ = ["MemoryFS"]
