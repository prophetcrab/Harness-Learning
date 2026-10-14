"""文件系统接缝的实现 —— LocalFS / MemoryFS / WorkspaceJailFS。

M5（P2 `_04`/`_05`）原样沿用：协议（protocol.py）+ 三个可互换实现。
"""

from components.fs.jail import WorkspaceJailFS
from components.fs.local import LocalFS
from components.fs.memory import MemoryFS
from components.fs.protocol import (
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_NOT_A_DIR,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    FileSystem,
    FsError,
)

__all__ = [
    "FS_EDIT_AMBIGUOUS",
    "FS_EDIT_NO_MATCH",
    "FS_NOT_A_DIR",
    "FS_NOT_A_FILE",
    "FS_NOT_FOUND",
    "FileSystem",
    "FsError",
    "LocalFS",
    "MemoryFS",
    "WorkspaceJailFS",
]
