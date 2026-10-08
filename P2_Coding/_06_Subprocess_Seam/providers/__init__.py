"""providers —— M5 能力接缝（阶段 `_06` 的顶层模块；累计自 `_04`/`_05`）。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级；`harness/` 冻结为 M1–M3
基线库，本阶段一行未改。本包机制（累计推进）：

- `_04`：把文件工具重构成 **FileSystem 三角色接缝**（Definition + Provider +
  Consumer），给出两个可互换实现（LocalFS / MemoryFS）——换 provider 换行为；
- `_05`：再加一个**策略型** provider `WorkspaceJailFS`——装饰任意 FileSystem，
  把"改动"（写/编辑）关进工作区；越界抛 `FS_SANDBOX_DENIED`（与 dsh 同名），
  经工具层翻译成与其他文件错误同构的结构化结果；
- `_06`：给**命令执行**建第二条接缝 `SubprocessService`（定义 + LocalSubprocess
  真跑进程 + ScriptedSubprocess 剧本回放），消费者是 `shell` 工具——同一套
  三角色 + 策略分层的模式，换一个能力重演一遍。

两条接缝的三角色（对应学习计划 §3 铁律 #5）：

    Definition   filesystem.py / subprocess.py   协议 + 稳定错误码
    Provider     local.py       LocalFS（真实磁盘）
                 memory.py      MemoryFS（内存）
                 jail.py        WorkspaceJailFS（fs 策略：只许区内改动）★ `_05`
                 subprocess_local.py     LocalSubprocess（真实进程）          ★ `_06`
                 subprocess_scripted.py  ScriptedSubprocess（剧本，零进程）   ★ `_06`
    Consumer     toolbox.py      read/write/list + **shell** 工具             ★ `_06`

另有 service.py：单槽能力容器（重复注册报错、显式 resolve，铁律 #5/#6），
一个容器可装多个能力（`fs` / `subprocess` 各占一槽）。

用法（在阶段目录下）：

    services = ServiceContainer()
    services.register(FS_CAPABILITY, WorkspaceJailFS(LocalFS("workspace")))
    services.register(SUBPROCESS_CAPABILITY, LocalSubprocess())
    fs = services.resolve(FS_CAPABILITY)                                      # ★ 显式解析
    shell_svc = services.resolve(SUBPROCESS_CAPABILITY)
    registry = build_toolbox(fs, shell_svc, workspace="workspace")            # 工具只认协议

依赖方向：`providers → harness`（工具基础设施：registry/schema/vocabulary）。
本包不依赖 prompt / context（那是提示词侧的机制，互不相干）。
"""

from providers.filesystem import (
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_NOT_A_DIR,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    FS_SANDBOX_DENIED,
    FileSystem,
    FsError,
)
from providers.jail import WorkspaceJailFS
from providers.local import LocalFS
from providers.memory import MemoryFS
from providers.service import FS_CAPABILITY, SUBPROCESS_CAPABILITY, ServiceContainer
from providers.subprocess import (
    SHELL_NONZERO_EXIT,
    SHELL_SPAWN_FAILED,
    SHELL_TIMEOUT,
    CommandResult,
    SubprocessError,
    SubprocessRequest,
    SubprocessService,
)
from providers.subprocess_local import LocalSubprocess
from providers.subprocess_scripted import ScriptedSubprocess
from providers.toolbox import ShellArgs, build_filesystem_registry, build_toolbox

__all__ = [
    # 定义：FileSystem
    "FileSystem",
    "FsError",
    "FS_NOT_FOUND",
    "FS_NOT_A_FILE",
    "FS_NOT_A_DIR",
    "FS_EDIT_NO_MATCH",
    "FS_EDIT_AMBIGUOUS",
    "FS_SANDBOX_DENIED",
    # 定义：Subprocess
    "SubprocessService",
    "SubprocessError",
    "SubprocessRequest",
    "CommandResult",
    "SHELL_SPAWN_FAILED",
    "SHELL_NONZERO_EXIT",
    "SHELL_TIMEOUT",
    # provider（fs：机制型 + 策略型）
    "LocalFS",
    "MemoryFS",
    "WorkspaceJailFS",
    # provider（subprocess：真实 + 剧本）
    "LocalSubprocess",
    "ScriptedSubprocess",
    # 服务容器
    "ServiceContainer",
    "FS_CAPABILITY",
    "SUBPROCESS_CAPABILITY",
    # 消费者
    "ShellArgs",
    "build_toolbox",
    "build_filesystem_registry",
]
