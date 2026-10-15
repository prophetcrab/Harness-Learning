"""tools 插件 —— 把能力组装成工具面（`tools` 服务）。

这是"消费者"插件：`inject = ("fs",)` 声明必需依赖；命令执行是可选的
（`ctx.get("subprocess")`）。工具实现只调用协议，不认识 LocalFS / 真进程。

    calculate            安全计算器（不依赖任何服务）
    read_file            fs.read_text
    write_file           fs.write_text（needs_approval）
    list_files           fs.list_files
    edit_file            fs.edit_text（old 恰好一次；needs_approval）
    search_text          fs.list_files + read_text（正则搜索）
    find_files           fs.list_files + fnmatch
    shell                subprocess.run（可选；needs_approval；cwd/期限/上限在此定死）

**卸载回卷**：工具面不只是个服务——注册进 ToolRegistry 的每个工具都要在
卸载时注销（`ctx.effect` 的清理阶段做），否则"换一套工具面"会撞上重名。
这正是"注册即 effect"在**外部注册表**上的手工版：ctx 管不到 ToolRegistry
的内部状态，插件自己把"撤销动作"写进 effect。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from components.fs import FileSystem, FsError
from components.proc import (
    SHELL_NONZERO_EXIT,
    SHELL_TIMEOUT,
    CommandResult,
    SubprocessError,
    SubprocessService,
)
from components.tools import (
    CalculateArgs,
    EditFileArgs,
    FindFilesArgs,
    ListFilesArgs,
    ReadFileArgs,
    SearchTextArgs,
    ShellArgs,
    ToolRegistry,
    WriteFileArgs,
    calculate,
    define_tool,
    schema_from_model,
)
from kernel import Context
from plugins.services import FS_SERVICE, SUBPROCESS_SERVICE, TOOLS_SERVICE


def _as_tool_error(exc: FsError) -> dict[str, Any]:
    """FsError → 给模型的结构化错误（人话 + 稳定码）。"""
    return {"error": str(exc), "code": exc.code}


def _bounded(text: str, cap: int) -> str:
    """输出上限：超长截断并附说明（防止单条事件把会话日志炸掉）。"""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…（输出过长已截断；原始长度 {len(text)} 字符）"


def _register_fs_tools(registry: ToolRegistry, fs: FileSystem) -> None:
    def read_file(path: str) -> dict[str, Any]:
        try:
            return {"path": path, "content": fs.read_text(path)}
        except FsError as exc:
            return _as_tool_error(exc)

    def write_file(path: str, content: str) -> dict[str, Any]:
        try:
            fs.write_text(path, content)
        except FsError as exc:
            return _as_tool_error(exc)
        return {"written": path, "bytes": len(content.encode("utf-8"))}

    def list_files(path: str = ".") -> dict[str, Any]:
        try:
            return {"path": path, "files": fs.list_files(path)}
        except FsError as exc:
            return _as_tool_error(exc)

    def edit_file(path: str, old: str, new: str) -> dict[str, Any]:
        try:
            fs.edit_text(path, old, new)
        except FsError as exc:
            return _as_tool_error(exc)
        return {"edited": path, "removed": len(old), "added": len(new)}

    registry.register(
        define_tool(
            "read_file",
            "读取工作区内某个文件的文本内容。路径是相对工作区根目录的相对路径。",
            schema_from_model(ReadFileArgs),
        ),
        read_file,
    )
    registry.register(
        define_tool(
            "write_file",
            "把文本写入工作区内的文件（会创建父目录）。写入前需要人工审批。",
            schema_from_model(WriteFileArgs),
            needs_approval=True,
        ),
        write_file,
    )
    registry.register(
        define_tool(
            "list_files",
            "列出工作区内某个目录下的所有文件（相对路径）。",
            schema_from_model(ListFilesArgs),
        ),
        list_files,
    )
    registry.register(
        define_tool(
            "edit_file",
            "把工作区内文件里的一段原文替换成新文本（old 必须恰好出现一次，"
            "否则报错）。修改文件优先用它，而不是整文件重写。修改前需要人工审批。",
            schema_from_model(EditFileArgs),
            needs_approval=True,
        ),
        edit_file,
    )


def _register_search_tools(registry: ToolRegistry, fs: FileSystem) -> None:
    def search_text(pattern: str, path: str = ".", max_results: int = 50) -> dict[str, Any]:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return {"error": f"正则表达式不合法：{exc}", "code": "INVALID_PATTERN"}
        try:
            files = fs.list_files(path)
        except FsError as exc:
            return _as_tool_error(exc)

        matches: list[dict[str, Any]] = []
        truncated = False
        for file_path in files:
            try:
                content = fs.read_text(file_path)
            except FsError:
                continue  # 列出后被删/二进制等：跳过，不炸整个检索
            for line_number, line in enumerate(content.splitlines(), start=1):
                if compiled.search(line):
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append(
                        {"file": file_path, "line": line_number, "text": line.strip()[:200]}
                    )
            if truncated:
                break
        result: dict[str, Any] = {"pattern": pattern, "path": path, "matches": matches}
        if truncated:
            result["truncated"] = f"结果超过 {max_results} 条已截断；请收窄 pattern 或 path"
        return result

    def find_files(pattern: str, path: str = ".", max_results: int = 200) -> dict[str, Any]:
        try:
            files = fs.list_files(path)
        except FsError as exc:
            return _as_tool_error(exc)
        matched = [
            file_path
            for file_path in files
            if fnmatch.fnmatch(file_path, pattern)
            or fnmatch.fnmatch(file_path.split("/")[-1], pattern)
        ]
        truncated = len(matched) > max_results
        result: dict[str, Any] = {"pattern": pattern, "path": path, "files": matched[:max_results]}
        if truncated:
            result["truncated"] = f"结果超过 {max_results} 个已截断"
        return result

    registry.register(
        define_tool(
            "search_text",
            "在工作区文件内容里按正则搜索，返回 文件+行号+该行文本。找代码、找 TODO、"
            "定位位置时用它；比逐个 read_file 快。",
            schema_from_model(SearchTextArgs),
        ),
        search_text,
    )
    registry.register(
        define_tool(
            "find_files",
            "按文件名通配模式（如 '*.py'、'test_*.py'）在工作区内找文件，返回相对路径列表。",
            schema_from_model(FindFilesArgs),
        ),
        find_files,
    )


def _register_shell_tool(
    registry: ToolRegistry,
    service: SubprocessService,
    *,
    cwd: str | None,
    timeout: float,
    max_output_chars: int,
) -> None:
    def shell(command: str) -> dict[str, Any]:
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

    registry.register(
        define_tool(
            "shell",
            (
                "在工作目录下执行一条命令行并返回退出码、标准输出与标准错误。"
                f"命令不能更改工作目录；最多运行 {timeout:g} 秒（超时会被终止）。"
                "执行命令前需要人工审批。"
            ),
            schema_from_model(ShellArgs),
            needs_approval=True,  # 执行任意命令 ≥ 写文件，同样先进审批
        ),
        shell,
    )


class ToolsPlugin:
    """按已就绪的能力组装工具面（inject 保证 fs 一定在）。"""

    name = "tools"
    inject = (FS_SERVICE,)
    #: 可选：命令执行能力（缺失则 shell 工具不出现——一直是这个语义，
    #: 只是原先没写进声明里）。
    optional = (SUBPROCESS_SERVICE,)

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        shell_timeout: float = 30.0,
        shell_max_output_chars: int = 20_000,
    ) -> None:
        self._workspace = Path(workspace) if workspace is not None else None
        self._shell_timeout = shell_timeout
        self._shell_max_output_chars = shell_max_output_chars

    def setup(self, ctx: Context) -> None:
        fs: FileSystem = ctx.require(FS_SERVICE)
        service: SubprocessService | None = ctx.get(SUBPROCESS_SERVICE)  # 可选能力
        registry = ToolRegistry()

        registry.register(
            define_tool(
                "calculate",
                "计算一个算术表达式并返回精确结果。涉及算术时必须用本工具，不要心算。",
                schema_from_model(CalculateArgs),
            ),
            calculate,
        )
        _register_fs_tools(registry, fs)
        _register_search_tools(registry, fs)
        if service is not None:
            _register_shell_tool(
                registry,
                service,
                cwd=str(self._workspace) if self._workspace is not None else None,
                timeout=self._shell_timeout,
                max_output_chars=self._shell_max_output_chars,
            )

        registered = list(registry.names)

        def dispose() -> None:
            # 卸载回卷：把本插件注册进外部注册表的工具全部注销。
            for name in registered:
                registry.unregister(name)

        ctx.provide(TOOLS_SERVICE, registry)
        return dispose


__all__ = ["ToolsPlugin"]
