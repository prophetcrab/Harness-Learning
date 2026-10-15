"""tool-shell 插件 —— 命令行执行工具（shell）。

**为什么它必须单独一个插件**：它依赖的是 `subprocess` 能力（真实进程），而
`subprocess` 本身是**可选装配**（离线组合用剧本命令、`--shell none` 干脆没有）。
如果 shell 工具挂在 tool-fs 里，就会出现"不想给命令执行能力时，连文件工具也
一起没了"——两个决定绑死。拆开后：装它就多一条命令通道，不装就干干净净没有
（工具面里连 `shell` 都不出现，模型也不会试图调它）。

**安全**：`needs_approval=True`（执行任意命令 ≥ 写文件），由管线问审批；
命令的工作目录在装配时定死（`cwd`），模型改不了。
"""

from __future__ import annotations

from typing import Any

from components.proc import (
    SHELL_NONZERO_EXIT,
    SHELL_TIMEOUT,
    CommandResult,
    SubprocessError,
    SubprocessService,
)
from components.tools import DeclarativeTool, tool
from kernel import Context
from plugins.services import SUBPROCESS_SERVICE, TOOLS_SERVICE


def _bounded(text: str, cap: int) -> str:
    """输出上限：超长截断并附说明（防止单条事件把会话日志炸掉）。"""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…（输出过长已截断；原始长度 {len(text)} 字符）"


def _build(
    service: SubprocessService,
    *,
    cwd: str | None,
    timeout: float,
    max_output_chars: int,
) -> tuple[DeclarativeTool, ...]:
    """按这一份 subprocess 服务造出 shell 工具（闭包绑定）。"""

    @tool("shell", needs_approval=True)
    def shell(command: str) -> dict[str, Any]:
        """在工作目录下执行一条命令行，返回退出码、标准输出与标准错误。

        参数 command：要执行的命令行（经系统 shell 解释，例如 'dir' 或
        'python -c "print(1+1)"'）。工作目录在装配时定死，不能指定其它目录。
        """
        try:
            result: CommandResult = service.run(command, cwd=cwd, timeout=timeout)
        except SubprocessError as exc:  # 基础设施失败：起不来
            return {"error": str(exc), "code": exc.code}

        stdout = _bounded(result.stdout, max_output_chars)
        stderr = _bounded(result.stderr, max_output_chars)
        if result.timed_out:
            return {
                "error": f"命令超过 {timeout:g} 秒未结束，已被终止",
                "code": SHELL_TIMEOUT,
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
            }
        if result.exit_code not in (0, None):
            return {
                "error": f"命令以退出码 {result.exit_code} 结束",
                "code": SHELL_NONZERO_EXIT,
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }

    return (shell,)


class ToolShellPlugin:
    """提供 `shell` 一件工具（inject subprocess + tools）。"""

    name = "tool-shell"
    category = "capability"
    inject = (SUBPROCESS_SERVICE, TOOLS_SERVICE)

    def __init__(
        self,
        *,
        cwd: str | None = None,
        timeout: float = 30.0,
        max_output_chars: int = 20_000,
    ) -> None:
        self._cwd = cwd
        self._timeout = timeout
        self._max_output_chars = max_output_chars

    def setup(self, ctx: Context):
        service: SubprocessService = ctx.require(SUBPROCESS_SERVICE)
        registry = ctx.require(TOOLS_SERVICE)

        built = _build(
            service,
            cwd=self._cwd,
            timeout=self._timeout,
            max_output_chars=self._max_output_chars,
        )
        for piece in built:
            registry.register(piece.definition, piece.func, owner=self.name)

        def dispose() -> None:
            for piece in built:
                registry.unregister(piece.name)

        return dispose


def tool_shell_plugin(
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
    max_output_chars: int = 20_000,
) -> ToolShellPlugin:
    """工厂：`cwd` 是命令的工作目录（装配决定，模型改不了）。"""
    return ToolShellPlugin(cwd=cwd, timeout=timeout, max_output_chars=max_output_chars)


__all__ = ["ToolShellPlugin", "tool_shell_plugin"]
