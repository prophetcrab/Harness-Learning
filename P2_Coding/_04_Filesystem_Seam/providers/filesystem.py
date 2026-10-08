"""FileSystem 接缝的 Service Definition —— 协议、错误词汇与稳定错误码。

M5 的第一个机制：把"文件读写"从"工具里直接调 pathlib"升级成一条**能力接缝**。
三角色（对应学习计划 §3 铁律 #5）：

    Definition（本文件）   协议：一个文件系统能做什么（read/write/edit/列表）
    Provider（local.py / memory.py）  实现：字节到底存哪、怎么存
    Consumer（toolbox.py） 消费者：read_file / write_file / list_files 工具

为什么要有这条缝：工具只依赖协议，换 provider 就换行为——
`LocalFS` 写真实磁盘、`MemoryFS` 只写内存；上层与测试都不感知差别
（"同一套工具测试在 local 与 memory 下全绿"就是它的验收形状）。

**错误模型**（与 dsh 的 FsError 同构）：provider 失败时抛 `FsError`，
携带一个**稳定的错误码**（如 `FS_NOT_FOUND`）与一段人话消息。调用方按 code
分支、不按消息文本分支；面向模型的工具层再把异常转成结构化结果
（`{"error": ..., "code": ...}`，铁律 #7：错误是给模型的输入）。

刻意**不含策略**：越界拒绝、只读保护之类的"规则"不属于本定义，也不属于基础
provider——它们是**策略型 provider**（如 `_05` 的 WorkspaceJailFS）的事。
接缝只回答"能做什么"，策略回答"允许做什么"。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 稳定错误码（调用方按 code 分支；新增码只增不改）
# ---------------------------------------------------------------------------

FS_NOT_FOUND = "FS_NOT_FOUND"            # 路径不存在
FS_NOT_A_FILE = "FS_NOT_A_FILE"          # 期望是文件，实际是目录
FS_NOT_A_DIR = "FS_NOT_A_DIR"            # 期望是目录，实际是文件
FS_EDIT_NO_MATCH = "FS_EDIT_NO_MATCH"    # edit：待替换的文本没找到
FS_EDIT_AMBIGUOUS = "FS_EDIT_AMBIGUOUS"  # edit：待替换的文本出现多次（拒绝猜）


class FsError(Exception):
    """文件系统操作的失败：稳定错误码 + 人话消息。

    例：FsError(FS_NOT_FOUND, "文件不存在：notes/a.txt")
    `str(exc)` 给出人话消息；机器判读用 `exc.code`。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Service Definition
# ---------------------------------------------------------------------------


@runtime_checkable
class FileSystem(Protocol):
    """文件系统能力协议。任何提供这四个方法的对象都满足它。

    与 LLMProvider 一样用 Protocol 而非抽象基类：实现者无需显式继承，
    第三方 provider 可以零依赖接入；`runtime_checkable` 让 isinstance 检查可行。

    路径约定：所有 path 都是**相对路径**（相对该 provider 自己的根：
    LocalFS 是真实目录、MemoryFS 是内存命名空间）。provider 负责把路径
    规范化；返回的列表项使用 `/` 分隔的 posix 风格相对路径。
    """

    def read_text(self, path: str) -> str:
        """读取一个文本文件；不存在/不是文件时抛 FsError（FS_NOT_FOUND / FS_NOT_A_FILE）。"""
        ...

    def write_text(self, path: str, content: str) -> None:
        """创建或整体覆盖一个文本文件（父目录按需创建）。

        对已存在的路径写入是**整体替换**——需要局部修改用 edit_text。
        """
        ...

    def edit_text(self, path: str, old: str, new: str) -> None:
        """把文件里的 old 字面文本替换为 new —— 恰出现一次才执行。

        old 未出现 → FsError(FS_EDIT_NO_MATCH)；出现多次 → FsError(FS_EDIT_AMBIGUOUS)
        （拒绝猜测替换哪一处）。这是"原子编辑"的最小版：读 → 定位 → 写回。
        """
        ...

    def list_files(self, path: str = ".") -> list[str]:
        """递归列出目录下的文件（posix 风格相对根的路径，排序后返回）。

        目录不存在 → FsError(FS_NOT_FOUND)；路径是文件 → FsError(FS_NOT_A_DIR)。
        """
        ...


__all__ = [
    "FileSystem",
    "FsError",
    "FS_NOT_FOUND",
    "FS_NOT_A_FILE",
    "FS_NOT_A_DIR",
    "FS_EDIT_NO_MATCH",
    "FS_EDIT_AMBIGUOUS",
]
