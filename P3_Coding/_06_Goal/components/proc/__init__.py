"""命令执行接缝的实现 —— LocalSubprocess / ScriptedSubprocess。

M5（P2 `_06`）原样沿用：协议（protocol.py）+ 两个可互换实现
（真实子进程 / 剧本回放）。包名用 `proc` 以免与标准库 subprocess 混淆。
"""

from components.proc.local import LocalSubprocess
from components.proc.protocol import (
    SHELL_NONZERO_EXIT,
    SHELL_SPAWN_FAILED,
    SHELL_TIMEOUT,
    CommandResult,
    SubprocessError,
    SubprocessRequest,
    SubprocessService,
)
from components.proc.scripted import ScriptedSubprocess

__all__ = [
    "SHELL_NONZERO_EXIT",
    "SHELL_SPAWN_FAILED",
    "SHELL_TIMEOUT",
    "CommandResult",
    "LocalSubprocess",
    "ScriptedSubprocess",
    "SubprocessError",
    "SubprocessRequest",
    "SubprocessService",
]
