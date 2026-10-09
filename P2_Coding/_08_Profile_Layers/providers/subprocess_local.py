"""LocalSubprocess —— SubprocessService 接缝的本地实现（Provider 角色）。

用标准库 `subprocess` 真正启动子进程：经平台 shell 解释命令（Windows = cmd.exe、
POSIX = /bin/sh），捕获 stdout / stderr，处理超时。它就是"本机执行"：只提供机制
（怎么把命令跑起来、怎么把结果收回来），**不做策略**——什么命令允许跑，是
上层（消费者 / 策略型 provider）的事。

四个实现细节值得留意：

1. **超时是结果**：超时后杀掉进程、把已捕获输出带回，返回
   `CommandResult(timed_out=True)`——接缝的词汇里"跑超时"与"跑完"一样是正常
   结局（对齐 dsh 的 ctx.shell）。
2. **超时杀的是整棵进程树**：`shell=True` 下的直接子进程是 shell，命令本体
   是它的子进程；只杀 shell 会让命令本体继续运行、还占着输出管道，导致捕获
   迟迟不返回（Windows 上实测：设 0.6 秒的超时要等 30 秒才收场）。
   Windows 用 `taskkill /F /T`、POSIX 用进程组（`start_new_session` +
   `killpg`）——terminate the full managed process range（dsh 的做法）。
   代价：被硬杀的进程还留在缓冲区里、未刷出的输出会丢（"杀"的固有语义）；
   已刷出的部分照常带回。
3. **环境剥除**：子进程默认继承本进程环境（含 `DEEPSEEK_API_KEY` 等凭证），
   这对"模型能任意跑命令"的场景是泄密面。`blocked_env` 在启动前剔掉这些键
   （dsh 的子进程同样"removes ambient credentials"）。注意 Windows 上
   至少需要保留 `SystemRoot` 否则 cmd 起不来——所以是"黑名单"而非白名单。
4. **启动失败才是异常**：cwd 不存在等问题在启动时抛 `OSError`，这里统一翻成
   `SubprocessError(SHELL_SPAWN_FAILED)`——与"命令跑起来后失败"（结果）区分开。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from providers.subprocess import (
    SHELL_SPAWN_FAILED,
    CommandResult,
    SubprocessError,
)

# 默认从子进程环境里剔掉的变量名（凭证不进任意子进程）。
DEFAULT_BLOCKED_ENV = ("DEEPSEEK_API_KEY",)


def _to_text(value: str | bytes | None) -> str:
    """把超时路径上可能出现的 bytes 输出归一到文本。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _terminate_tree(process: subprocess.Popen) -> None:
    """杀掉整棵子进程树（超时用）。失败时退回杀直接子进程。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
            return
        except OSError:  # taskkill 不可用（罕见）：退回 kill
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    process.kill()


class LocalSubprocess:
    """以本机 shell 执行命令（SubprocessService 协议实现）。"""

    def __init__(self, *, blocked_env: tuple[str, ...] = DEFAULT_BLOCKED_ENV) -> None:
        self._blocked_env = tuple(blocked_env)

    @property
    def blocked_env(self) -> tuple[str, ...]:
        """被剥除的环境变量名（只读）。"""
        return self._blocked_env

    def _child_env(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if key not in self._blocked_env}

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        popen_kwargs: dict = {
            "shell": True,  # 经平台 shell 解释（与"模型在终端里敲的命令"同义）
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": self._child_env(),
        }
        if os.name != "nt":
            # POSIX：自成进程组，超时时可整组杀掉（与 Windows 的 taskkill /T 对应）。
            popen_kwargs["start_new_session"] = True
        if cwd is not None:
            popen_kwargs["cwd"] = str(Path(cwd))

        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            # 基础设施失败（如 cwd 不存在、shell 起不来）：异常路径。
            raise SubprocessError(SHELL_SPAWN_FAILED, f"无法启动命令：{exc}") from exc

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_tree(process)  # ★ 杀整棵树，它没跑完就是结果（不是异常）
            stdout, stderr = process.communicate()
            return CommandResult(
                exit_code=None,
                stdout=_to_text(stdout),
                stderr=_to_text(stderr),
                timed_out=True,
            )
        return CommandResult(
            exit_code=process.returncode,
            stdout=_to_text(stdout),
            stderr=_to_text(stderr),
            timed_out=False,
        )


__all__ = ["DEFAULT_BLOCKED_ENV", "LocalSubprocess"]
