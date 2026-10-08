"""providers —— M5 能力接缝（阶段 `_04` 新增的顶层模块）。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级；`harness/` 冻结为 M1–M3
基线库，本阶段一行未改。本阶段机制一句话：

    把文件工具从「直接操作 pathlib」重构成 **FileSystem 三角色接缝**，
    并给出两个可互换实现（LocalFS / MemoryFS）——换 provider 换行为，上层无感。

三角色（对应学习计划 §3 铁律 #5）：

    Definition   filesystem.py   FileSystem 协议 + FsError 稳定错误码
    Provider     local.py        LocalFS（真实磁盘）
                 memory.py       MemoryFS（内存）
    Consumer     toolbox.py      read_file / write_file / list_files 工具

另有 service.py：单槽能力容器（重复注册报错、显式 resolve，铁律 #5/#6），
让"用哪个 provider"在一个明确的解析点决定，而不是藏在工具函数里。

用法（在阶段目录下）：

    services = ServiceContainer()
    services.register(FS_CAPABILITY, LocalFS("workspace"))   # 或 MemoryFS()
    fs = services.resolve(FS_CAPABILITY)                     # ★ 显式解析
    registry = build_filesystem_registry(fs)                 # 工具只认协议

依赖方向：`providers → harness`（工具基础设施：registry/schema/vocabulary）。
本包不依赖 prompt / context（那是提示词侧的机制，互不相干）。
"""

from providers.filesystem import (
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_NOT_A_DIR,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    FileSystem,
    FsError,
)
from providers.local import LocalFS
from providers.memory import MemoryFS
from providers.service import FS_CAPABILITY, ServiceContainer
from providers.toolbox import build_filesystem_registry

__all__ = [
    # 定义
    "FileSystem",
    "FsError",
    "FS_NOT_FOUND",
    "FS_NOT_A_FILE",
    "FS_NOT_A_DIR",
    "FS_EDIT_NO_MATCH",
    "FS_EDIT_AMBIGUOUS",
    # provider
    "LocalFS",
    "MemoryFS",
    # 服务容器
    "ServiceContainer",
    "FS_CAPABILITY",
    # 消费者
    "build_filesystem_registry",
]
