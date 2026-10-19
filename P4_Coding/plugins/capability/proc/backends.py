"""三个命令执行后端（能力插件）：Git Bash / POSIX / 剧本。

每个后端只做一件事：把自己注册进 `proc` 服务。选哪个由 `ProcRuntime.select()`
按 `priority` + `available()` 决定——同平台上不会选错方言。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import Context

_CHUNK = 4096


def _decode(data: bytes) -> str:
    """UTF-8 优先、GBK 兜底、最后替换——中文 Windows 的乱码来源就在这里。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize_newlines(text: str) -> str:
    """CRLF → LF（模型看到的行号/正则才稳定）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _LocalBackend:
    """本地进程执行的公共部分（子类只决定 argv 与环境）。"""

    name = "local"
    #: 需要的平台（"nt" 或 "posix"）。
    platform = "nt"

    def available(self) -> bool:
        return (os.name == "nt") == (self.platform == "nt") and self._shell_available()

    def _shell_available(self) -> bool:
        return True

    def _argv(self, command: str) -> Sequence[str]:
        raise NotImplementedError

    def _cwd(self, cwd: Path) -> str:
        return str(cwd)

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        idle_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        argv = list(self._argv(command))
        merged = {**os.environ, **DEFAULT_ENV, **dict(env or {})}
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self._cwd(cwd),
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # POSIX：自成进程组，超时时整组杀掉（对应 Windows 的 taskkill /T）。
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            return ProcResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=f"无法启动后端 {self.name!r}：{exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                argv=tuple(argv),

            )
        out: list[bytes] = []
        err: list[bytes] = []
        last_output = [time.monotonic()]
        lock = threading.Lock()

        def pump(stream, sink: list[bytes]) -> None:
            while True:
                chunk = stream.read1(_CHUNK) if hasattr(stream, "read1") else stream.read(_CHUNK)
                if not chunk:
                    return
                with lock:
                    sink.append(chunk)
                    last_output[0] = time.monotonic()

        readers = [
            threading.Thread(target=pump, args=(process.stdout, out), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, err), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        kind = None
        while process.poll() is None:
            now = time.monotonic()
            if now - started > timeout:
                timed_out, kind = True, "hard"
                break
            if idle_timeout is not None and now - last_output[0] > idle_timeout:
                timed_out, kind = True, "idle"
                break
            time.sleep(0.02)

        if timed_out:
            _kill_tree(process)
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)

        with lock:
            stdout = _normalize_newlines(_decode(b"".join(out)))
            stderr = _normalize_newlines(_decode(b"".join(err)))
        duration = int((time.monotonic() - started) * 1000)
        status = "timeout" if timed_out else ("ok" if process.returncode == 0 else "error")
        return ProcResult(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            timeout_kind=kind,
            argv=tuple(argv),


        )

def _kill_tree(process: subprocess.Popen) -> None:
    """杀整棵进程树（只杀直接子进程的话，命令本体还在跑）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass

# === 本地副本（来自 plugins/base/proc.py；插件之间不 import，见决策 0053）===
class GitBashBackend(_LocalBackend):
    """Windows 上的默认后端：Git Bash（POSIX 语法）。"""

    name = "git-bash"
    platform = "nt"
    priority = 100
    dialect = "posix"

    #: 常见安装位置（按顺序找）。
    CANDIDATES = (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",

    )
    def __init__(self, bash_path: str | None = None) -> None:
        self._bash = bash_path or self._find_bash()

    def _find_bash(self) -> str:
        for candidate in self.CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        return "bash"  # 交给 PATH

    def _shell_available(self) -> bool:
        return Path(self._bash).is_file() or self._bash == "bash"

    def _argv(self, command: str) -> Sequence[str]:
        # --noprofile --norc：跳过用户配置（启动快、行为可复现）。
        return [self._bash, "--noprofile", "--norc", "-c", command]

class PosixBackend(_LocalBackend):
    """Linux / macOS 后端：sh -c。"""

    name = "posix"
    platform = "posix"
    priority = 100
    dialect = "posix"

    def _argv(self, command: str) -> Sequence[str]:
        return ["sh", "-c", command]

class ScriptedBackend:
    """剧本后端（离线测试）：按顺序返回预设结果，并记录收到的命令。"""

    name = "scripted"
    priority = 0
    dialect = "unknown"

    def __init__(self, results: Sequence[ProcResult | Mapping[str, object]] | None = None) -> None:
        self._results = list(results or [])
        self._index = 0
        #: 收到的 (command, cwd, timeout)。
        self.calls: list[tuple[str, Path, float]] = []

    def available(self) -> bool:
        return True

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        idle_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        self.calls.append((command, cwd, timeout))
        if not self._results:
            return ProcResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=f"剧本后端没有预设结果（第 {len(self.calls)} 次调用）",
                duration_ms=0,
            )
        index = min(self._index, len(self._results) - 1)
        self._index += 1
        item = self._results[index]
        if isinstance(item, ProcResult):
            return item
        return ProcResult(
            status=str(item.get("status", "ok")),  # type: ignore[arg-type]
            exit_code=item.get("exit_code"),  # type: ignore[arg-type]
            stdout=str(item.get("stdout", "")),
            stderr=str(item.get("stderr", "")),
            duration_ms=int(item.get("duration_ms", 0)),

        )
@dataclass(frozen=True)
class ProcResult:
    """一次命令执行的结果（超时时带上**已经拿到的部分输出**）。"""

    status: ProcStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    timeout_kind: TimeoutKind | None = None
    #: 实际执行的命令行（排障用：看得出走的是哪个后端、哪门方言）。
    argv: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "timeout_kind": self.timeout_kind,
        }

DEFAULT_ENV: dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

def win_to_posix_path(path: str | Path) -> str:
    """`D:\\project\\x` → `/d/project/x`（Git Bash 认这种写法）。"""
    text = str(path)
    if len(text) >= 2 and text[1] == ":":
        drive = text[0].lower()
        rest = text[2:].replace("\\", "/")
        if not rest.startswith("/"):
            rest = "/" + rest
        return f"/{drive}{rest}"
    return text.replace("\\", "/")

# --- 服务/事件名（字符串，本地复刻）---
PROC_SERVICE = "proc"



class ProcGitBashPlugin:
    """Windows 默认后端：Git Bash（POSIX 语法、UTF-8、进程树杀）。"""

    name = "proc-local-git-bash"
    inject = (PROC_SERVICE,)

    def __init__(self, *, bash_path: str | None = None) -> None:
        self._bash = bash_path

    def setup(self, ctx: Context):
        runtime = ctx.require(PROC_SERVICE)
        return runtime.register(GitBashBackend(self._bash))

class ProcPosixPlugin:
    """Linux / macOS 后端：sh -c。"""

    name = "proc-local-posix"
    inject = (PROC_SERVICE,)

    def setup(self, ctx: Context):
        runtime = ctx.require(PROC_SERVICE)
        return runtime.register(PosixBackend())

class ProcScriptedPlugin:
    """剧本后端（离线测试）：不执行真实命令，按预设返回。"""

    name = "proc-scripted"
    inject = (PROC_SERVICE,)

    def __init__(self, *, results: list[dict[str, Any]] | None = None) -> None:
        self._results = results or []

    def setup(self, ctx: Context):
        runtime = ctx.require(PROC_SERVICE)
        return runtime.register(ScriptedBackend(self._results))

def proc_git_bash_plugin(**config: Any) -> ProcGitBashPlugin:
    return ProcGitBashPlugin(**config)

def proc_posix_plugin(**config: Any) -> ProcPosixPlugin:
    return ProcPosixPlugin(**config)

def proc_scripted_plugin(**config: Any) -> ProcScriptedPlugin:
    return ProcScriptedPlugin(**config)

__all__ = [
    "ProcGitBashPlugin",
    "ProcPosixPlugin",
    "ProcScriptedPlugin",
    "proc_git_bash_plugin",
    "proc_posix_plugin",
    "proc_scripted_plugin",
]

class _LocalBackend:
    """本地进程执行的公共部分（子类只决定 argv 与环境）。"""

    name = "local"
    #: 需要的平台（"nt" 或 "posix"）。
    platform = "nt"

    def available(self) -> bool:
        return (os.name == "nt") == (self.platform == "nt") and self._shell_available()

    def _shell_available(self) -> bool:
        return True

    def _argv(self, command: str) -> Sequence[str]:
        raise NotImplementedError

    def _cwd(self, cwd: Path) -> str:
        return str(cwd)

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        idle_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        argv = list(self._argv(command))
        merged = {**os.environ, **DEFAULT_ENV, **dict(env or {})}
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self._cwd(cwd),
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # POSIX：自成进程组，超时时整组杀掉（对应 Windows 的 taskkill /T）。
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            return ProcResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=f"无法启动后端 {self.name!r}：{exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                argv=tuple(argv),

            )
        out: list[bytes] = []
        err: list[bytes] = []
        last_output = [time.monotonic()]
        lock = threading.Lock()

        def pump(stream, sink: list[bytes]) -> None:
            while True:
                chunk = stream.read1(_CHUNK) if hasattr(stream, "read1") else stream.read(_CHUNK)
                if not chunk:
                    return
                with lock:
                    sink.append(chunk)
                    last_output[0] = time.monotonic()

        readers = [
            threading.Thread(target=pump, args=(process.stdout, out), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, err), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        kind = None
        while process.poll() is None:
            now = time.monotonic()
            if now - started > timeout:
                timed_out, kind = True, "hard"
                break
            if idle_timeout is not None and now - last_output[0] > idle_timeout:
                timed_out, kind = True, "idle"
                break
            time.sleep(0.02)

        if timed_out:
            _kill_tree(process)
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)

        with lock:
            stdout = _normalize_newlines(_decode(b"".join(out)))
            stderr = _normalize_newlines(_decode(b"".join(err)))
        duration = int((time.monotonic() - started) * 1000)
        status = "timeout" if timed_out else ("ok" if process.returncode == 0 else "error")
        return ProcResult(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            timeout_kind=kind,
            argv=tuple(argv),


        )

def _kill_tree(process: subprocess.Popen) -> None:
    """杀整棵进程树（只杀直接子进程的话，命令本体还在跑）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass

# === 本地副本（来自 plugins/base/proc.py）===
class _LocalBackend:
    """本地进程执行的公共部分（子类只决定 argv 与环境）。"""

    name = "local"
    #: 需要的平台（"nt" 或 "posix"）。
    platform = "nt"

    def available(self) -> bool:
        return (os.name == "nt") == (self.platform == "nt") and self._shell_available()

    def _shell_available(self) -> bool:
        return True

    def _argv(self, command: str) -> Sequence[str]:
        raise NotImplementedError

    def _cwd(self, cwd: Path) -> str:
        return str(cwd)

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        idle_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        argv = list(self._argv(command))
        merged = {**os.environ, **DEFAULT_ENV, **dict(env or {})}
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self._cwd(cwd),
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # POSIX：自成进程组，超时时整组杀掉（对应 Windows 的 taskkill /T）。
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            return ProcResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=f"无法启动后端 {self.name!r}：{exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
                argv=tuple(argv),

            )
        out: list[bytes] = []
        err: list[bytes] = []
        last_output = [time.monotonic()]
        lock = threading.Lock()

        def pump(stream, sink: list[bytes]) -> None:
            while True:
                chunk = stream.read1(_CHUNK) if hasattr(stream, "read1") else stream.read(_CHUNK)
                if not chunk:
                    return
                with lock:
                    sink.append(chunk)
                    last_output[0] = time.monotonic()

        readers = [
            threading.Thread(target=pump, args=(process.stdout, out), daemon=True),
            threading.Thread(target=pump, args=(process.stderr, err), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        kind = None
        while process.poll() is None:
            now = time.monotonic()
            if now - started > timeout:
                timed_out, kind = True, "hard"
                break
            if idle_timeout is not None and now - last_output[0] > idle_timeout:
                timed_out, kind = True, "idle"
                break
            time.sleep(0.02)

        if timed_out:
            _kill_tree(process)
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=2)

        with lock:
            stdout = _normalize_newlines(_decode(b"".join(out)))
            stderr = _normalize_newlines(_decode(b"".join(err)))
        duration = int((time.monotonic() - started) * 1000)
        status = "timeout" if timed_out else ("ok" if process.returncode == 0 else "error")
        return ProcResult(
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            timeout_kind=kind,
            argv=tuple(argv),

        )

def _kill_tree(process: subprocess.Popen) -> None:
    """杀整棵进程树（只杀直接子进程的话，命令本体还在跑）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass

# --- 本地类型别名 ---
ProcStatus = str
TimeoutKind = str
