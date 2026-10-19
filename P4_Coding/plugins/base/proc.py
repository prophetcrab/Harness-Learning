"""proc —— 命令执行层（基础插件）。

提供 `proc` 服务：**选择当前可用的后端**并转发执行。它自己不含平台细节——
Git Bash / POSIX / 剧本各是一个能力插件（`proc-local-git-bash` 等），
和 `llm` 与 provider 的关系同构。

    proc.run("ls", cwd=workspace.root, timeout=30) -> ProcResult
    proc.backends() / proc.info()

**方言检查**：执行前看一眼命令是不是写成了别的方言（cmd / PowerShell），
是就**拒绝并给纠正提示**——不做自动翻译（翻错了可能造成破坏）。

**不要求后端齐全**：没有后端时 `run()` 返回 NO_BACKEND 错误结果；
是否"必须有某个后端"由装配单决定（`requires` 也可以加在装配侧）。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from core import Context, Disposer

#: 执行服务的名字。
PROC_SERVICE = "proc"


class ProcRuntime:
    """后端注册表 + 执行入口。"""

    def __init__(self) -> None:
        self._backends: dict[str, ProcBackend] = {}

    # ------------------------------------------------------------------
    # 后端
    # ------------------------------------------------------------------

    def register(self, backend: ProcBackend) -> Disposer:
        """注册一个后端；同优先级取先注册的。返回注销句柄（幂等）。"""
        name = getattr(backend, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError(f"后端必须有非空 name：{backend!r}")
        if name in self._backends:
            raise ValueError(f"后端已注册：{name!r}（不静默覆盖）")
        self._backends[name] = backend

        def unregister() -> None:
            if self._backends.get(name) is backend:
                self._backends.pop(name, None)

        return unregister

    def backends(self) -> tuple[str, ...]:
        return tuple(self._backends)

    def select(self) -> ProcBackend | None:
        """当前可用后端里优先级最高的那个。"""
        available = [
            item for item in self._backends.values() if _is_available(item)
        ]
        if not available:
            return None
        return max(available, key=lambda item: getattr(item, "priority", 0))

    def info(self) -> dict[str, Any]:
        selected = self.select()
        return {
            "backends": list(self._backends),
            "selected": getattr(selected, "name", None),
            "dialect": getattr(selected, "dialect", "unknown") if selected else None,
        }

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        timeout: float = 30.0,
        idle_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        """在选中后端上执行；没有后端 / 方言不符 → 错误结果（不抛异常）。"""
        backend = self.select()
        if backend is None:
            return ProcResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr="没有可用的命令执行后端（装上 proc-local-git-bash / "
                "proc-local-posix / proc-scripted 之一）",
                duration_ms=0,
            )
        expected = getattr(backend, "dialect", "unknown")
        if expected in ("posix", "cmd", "powershell"):
            hint = dialect_hint(command, expected)
            if hint is not None:
                return ProcResult(
                    status="error",
                    exit_code=None,
                    stdout="",
                    stderr=hint,
                    duration_ms=0,
                )
        target = Path(cwd) if cwd is not None else Path.cwd()
        if not target.is_dir():
            return ProcResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=f"工作目录不存在：{target}",
                duration_ms=0,
            )
        return backend.run(
            command,
            cwd=target,
            timeout=timeout,
            idle_timeout=idle_timeout,
            env=env,


        )
def _is_available(backend: ProcBackend) -> bool:
    check = getattr(backend, "available", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 —— 探测失败按不可用处理
        return False


class ProcPlugin:
    """提供 `proc` 服务。装配单里应声明 `role="base"`。"""

    name = "proc-service"
    #: 用户定义：三个后端都是必需依赖（不可用的后端仍可"在场"——按平台自报）。
    requires = ("proc-local-git-bash", "proc-local-posix", "proc-scripted")

    def setup(self, ctx: Context) -> None:
        ctx.provide(PROC_SERVICE, ProcRuntime())


def proc_plugin(**config: Any) -> ProcPlugin:
    """工厂（当前无配置）。"""
    return ProcPlugin(**config)


__all__ = [
    "DEFAULT_ENV",
    "Dialect",
    "GitBashBackend",
    "PROC_SERVICE",
    "PosixBackend",
    "ProcBackend",
    "ProcPlugin",
    "ProcResult",
    "ProcRuntime",
    "ProcStatus",
    "ScriptedBackend",
    "TimeoutKind",
    "detect_dialect",
    "dialect_hint",
    "proc_plugin",
    "win_to_posix_path",
]


# ======================================================================
# 以下合并自 components/proc/protocol.py
# ======================================================================


#: 执行结果状态。
ProcStatus = Literal["ok", "error", "timeout"]

#: 超时种类：没有这个区分，就无法说明"是卡住了还是只是慢"。
TimeoutKind = Literal["idle", "hard"]

#: 方言。
Dialect = Literal["posix", "cmd", "powershell", "unknown"]


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


class ProcBackend(Protocol):
    """一个执行后端（Git Bash / POSIX sh / 剧本 / 将来的容器）。"""

    name: str

    def available(self) -> bool:
        """这个后端在当前平台上能用吗（不能用的不参与选择）。"""
        ...

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        idle_timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        ...


# ---------------------------------------------------------------------------
# 方言识别
# ---------------------------------------------------------------------------

#: 强特征：命中即判（不判"中间地带"的命令——cd / echo / mkdir 三种方言都能跑）。
_DIALECT_PATTERNS: tuple[tuple[Dialect, re.Pattern[str]], ...] = (
    ("cmd", re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")),
    ("cmd", re.compile(r"(^|[;&|]\s*)(dir|copy|del|type|ren|md|rd|move|xcopy|set)\b", re.I)),
    ("cmd", re.compile(r"(^|\s)/(b|s|q|a)\s", re.I)),
    ("cmd", re.compile(r"2>\s*nul\b", re.I)),
    ("powershell", re.compile(r"\$env:[A-Za-z_]", re.I)),
    (
        "powershell",
        re.compile(
            r"(^|[;&|]\s*)(Get|Set|Remove|Select|Where|ForEach|Write|New|Test|Start|Stop)-[A-Za-z]+",
            re.I,
        ),
    ),
    ("powershell", re.compile(r"(^|\s)-(Path|Recurse|LiteralPath|Filter)\b", re.I)),
    ("powershell", re.compile(r"Write-Host|Write-Output|Out-File\b", re.I)),
    ("posix", re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")),
    ("posix", re.compile(r"(^|[;&|]\s*)(ls|grep|sed|awk|cat|rm|cp|mv|find|export)\b")),
    ("posix", re.compile(r"(^|\s)2>\s*/dev/null")),
    ("posix", re.compile(r"(^|\s)--?[a-z][a-z-]*=")),


)
def detect_dialect(command: str) -> Dialect:
    """认出命令的方言；中间地带 / 认不出 → `"unknown"`。

    顺序：先 cmd、再 PowerShell、最后 POSIX——强特征之间不重叠；
    一个命令同时命中两门方言时，取**先命中**的（说明命令本身就有问题，
    交给调用方报"写法混用"）。
    """
    text = command.strip()
    if not text:
        return "unknown"
    for dialect, pattern in _DIALECT_PATTERNS:
        if pattern.search(text):
            return dialect
    return "unknown"


def dialect_hint(command: str, expected: Dialect) -> str | None:
    """命令的方言与执行环境不符时，返回一句给模型的纠正提示；相符 → None。"""
    found = detect_dialect(command)
    if found in ("unknown", expected):
        return None
    equivalents = {
        ("cmd", "posix"): "ls -1 / cat / rm / cp / mkdir -p / $VAR",
        ("powershell", "posix"): "ls / cat / rm / cp / mkdir -p / $VAR",
        ("posix", "cmd"): "dir / type / del / copy / set VAR=",
        ("posix", "powershell"): "Get-ChildItem / Get-Content / Remove-Item / $env:VAR",
    }
    hint = equivalents.get((found, expected), "")
    return (
        f"这条命令看起来是 {found} 语法，但当前环境是 {expected}。"
        f"等价写法参考：{hint}。请改写后重试。"


    )


# ======================================================================
# 以下合并自 components/proc/local.py
# ======================================================================


#: 默认注入的环境变量：无缓冲 + UTF-8（不管调用方有没有传 env）。
DEFAULT_ENV: dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

#: 读取线程的块大小。
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

    # 注意：**不要**覆盖 `_cwd` 把路径转成 POSIX——`Popen(cwd=...)` 由 Windows
    # 解析，传 `/c/Users/...` 会得到 WinError 267（目录名无效）。bash 启动后
    # 自己会把当前目录呈现为 `/c/...`，命令里的相对路径照常工作。


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
