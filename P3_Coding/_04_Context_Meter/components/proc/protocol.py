"""SubprocessService 接缝的 Service Definition —— 协议、结果词汇与稳定错误码。

M5 的第三个机制（`_06` 唯一新增的东西）：给"命令执行"建一条能力接缝。三角色与前两条
接缝（`_04`/`_05` 的 FileSystem、P1 的 LLMProvider）同构：

    Definition（本文件）   协议：怎么跑一个命令、拿回什么
    Provider（local / scripted）  实现：真跑进程（subprocess）/ 剧本回放（测试）
    Consumer（toolbox.py） 消费者：`shell` 工具，模型看到的就是它的结果

**结果词汇（本文件的核心）**：一次命令执行的结局是一个 `CommandResult`——
退出码（或 None）、stdout、stderr、是否因超时被杀。关键设计（对齐 dsh 的
`ctx.shell`）：**非零退出、超时被杀都是"结果"，不是异常**——只有基础设施失败
（启动不了进程，如工作目录不存在）才抛 `SubprocessError`。

    为什么这样分：命令"跑完但失败"是模型需要理解的信息（去读 stderr、改命令重试），
    必须能被记录、被渲染；而"起不来"是环境问题，属于异常路径。混在一起会让
    "命令失败"变成不可控的中断。

超时的语义：由调用方（消费者）在请求里给出期限（dsh："callers own deadlines"）；
超时后进程被杀，`timed_out=True`、`exit_code=None`，已捕获的输出照常带回
（模型能看到"跑到哪一步被掐了"）。

刻意**不含策略**：命令白名单、目录限制之类的"允许跑什么"是策略层的事
（同 `_05` 的教训：机制回答"怎么做"，策略回答"允许做什么"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 稳定错误码（调用方按 code 分支；新增码只增不改）
# ---------------------------------------------------------------------------

# 接缝层（provider 抛 SubprocessError 时使用）：
SHELL_SPAWN_FAILED = "SHELL_SPAWN_FAILED"      # 进程起不来（工作目录不存在等基础设施问题）
# 工具层（消费者把"结果"渲染成给模型的错误时使用）：
SHELL_NONZERO_EXIT = "SHELL_NONZERO_EXIT"      # 命令跑完但退出码非零
SHELL_TIMEOUT = "SHELL_TIMEOUT"                # 命令超时被杀


class SubprocessError(Exception):
    """命令执行的**基础设施**失败：稳定错误码 + 人话消息。

    例：SubprocessError(SHELL_SPAWN_FAILED, "无法启动命令：工作目录不存在 …")
    注意：命令"跑起来之后"的失败（非零退出、超时）不走异常，走 CommandResult。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# 结果与请求词汇
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """一次命令执行的结局（无论成败都是"结果"）。

    exit_code  进程退出码；超时被杀时为 None（没有正常的退出码）。
    stdout     捕获的标准输出（文本；被超时截断的部分也带回）。
    stderr     捕获的标准错误（与 stdout 分开——模型要能分辨"结果是输出还是报错"）。
    timed_out  是否因超时被杀。
    """

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class SubprocessRequest:
    """一次命令执行请求（provider 收到的样子；测试断言与调试用）。"""

    command: str
    cwd: str | None = None
    timeout: float | None = None


# ---------------------------------------------------------------------------
# Service Definition
# ---------------------------------------------------------------------------


@runtime_checkable
class SubprocessService(Protocol):
    """命令执行能力协议。任何提供 run() 的对象都满足它。

    与 FileSystem / LLMProvider 一样用 Protocol 而非抽象基类：实现者无需显式
    继承，第三方 provider 可以零依赖接入；`runtime_checkable` 让 isinstance 可行。
    """

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """在 cwd 下执行 command（经平台 shell 解释），最多等 timeout 秒。

        返回 CommandResult（非零退出、超时被杀都是结果）；只有基础设施失败
        （如 cwd 不存在、shell 不可用）抛 SubprocessError(SHELL_SPAWN_FAILED)。
        timeout=None 表示不加期限——由调用方自己负责兜底（消费者默认会给期限）。
        """
        ...


__all__ = [
    "CommandResult",
    "SubprocessRequest",
    "SubprocessError",
    "SubprocessService",
    "SHELL_SPAWN_FAILED",
    "SHELL_NONZERO_EXIT",
    "SHELL_TIMEOUT",
]
