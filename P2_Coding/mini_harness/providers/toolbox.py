"""工具箱（Consumer 角色）—— 工具只依赖抽象，不依赖具体实现。

接缝的第三个角色（累计自 `_04`）：`read_file` / `write_file` / `list_files`
调用注入的 `FileSystem`（协议）而不碰 pathlib；`_06` 起新增 **`shell` 工具**，
调用注入的 `SubprocessService`（协议）而不直接 `subprocess.run`。

两个组装函数：

    build_toolbox(fs, subprocess=None, ...)      会话的完整工具面（`_11` 起为
                                                 "基本编码代理"补齐：calculate +
                                                 read/write/list/edit + search/find
                                                 + 可选 shell/搜索）
    build_filesystem_registry(fs, ...)           不带 shell 的入口（保留兼容）

`_11` 新增三个编码工具（都骑在既有接缝上，不引入新机制）：
    edit_file    精确局部替换（old 恰好一次；0/多次报错——防误改）
    search_text  文件内容正则搜索（文件+行号+该行；结果有上限）
    find_files   按文件名通配查找（'*.py' 等；结果有上限）

两处翻译（接缝词汇 → 工具词汇，铁律 #7"错误是给模型的输入"）：

1. `FsError`        → {"error": 人话, "code": 稳定码}
2. `SubprocessError`（基础设施失败）→ 同上；
   `CommandResult`（命令跑完的结局）按三条规则渲染：
     - 超时被杀        → error + SHELL_TIMEOUT（带回已捕获输出）
     - 退出码非零      → error + SHELL_NONZERO_EXIT（带回 stdout/stderr）
     - 正常结束        → 普通结果（exit_code + stdout + stderr）
   另外两处消费侧约束（请求发出前就定死，模型不可改）：
     - **工作目录** = 会话工作区（cwd 显式传给 provider）；
     - **期限与输出上限** = 组装时给定（"callers own deadlines"，对齐 dsh 的
       resolve 步骤：跑之前把 cwd / timeout / 输出上限都变成显式的）。
   输出超限不是错误：截断并附一行说明（防单条事件把会话日志炸掉）。

工具的定义（名字/说明/schema）沿用基线；只有实现被接缝化——"换实现不改上层"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.llm.tools import calculate
from harness.tools.registry import ToolRegistry, define_tool
from harness.tools.schema import schema_from_model
from harness.tools.workspace import (
    CalculateArgs,
    ListFilesArgs,
    ReadFileArgs,
    WriteFileArgs,
)
from providers.filesystem import FileSystem, FsError
from providers.subprocess import (
    SHELL_NONZERO_EXIT,
    SHELL_TIMEOUT,
    CommandResult,
    SubprocessError,
    SubprocessService,
)

# ---------------------------------------------------------------------------
# 参数模型（pydantic → JSON Schema）
# ---------------------------------------------------------------------------


class ShellArgs(BaseModel):
    command: str = Field(
        description=(
            "要在工作目录下执行的命令行（经系统 shell 解释，例如 'dir' 或 "
            "'python -c \"print(1+1)\"'）。工作目录与会话工作区一致，不能指定其它目录。"
        )
    )


class EditFileArgs(BaseModel):
    path: str = Field(description="相对工作区根目录的文件路径，例如 'src/app.py'")
    old: str = Field(
        description=(
            "要被替换的原文（必须与文件中的文本逐字符一致，且在文件里**恰好出现一次**）。"
            "出现零次或多次都会报错——这是防误改的保护。"
        )
    )
    new: str = Field(description="替换后的新文本（可以为空字符串，表示删除 old）")


class SearchTextArgs(BaseModel):
    pattern: str = Field(
        description="要搜索的正则表达式，例如 'def .*\\(' 或 'TODO'（区分大小写）"
    )
    path: str = Field(default=".", description="从工作区的哪个目录开始搜，默认整个工作区")
    max_results: int = Field(default=50, description="最多返回多少条匹配，默认 50")


class FindFilesArgs(BaseModel):
    pattern: str = Field(
        description="文件名的通配模式（shell 风格），例如 '*.py'、'test_*.py'、'notes/*'"
    )
    path: str = Field(default=".", description="从工作区的哪个目录开始找，默认整个工作区")
    max_results: int = Field(default=200, description="最多返回多少个文件，默认 200")


# ---------------------------------------------------------------------------
# 内部：三个注册块
# ---------------------------------------------------------------------------


def _as_tool_error(exc: FsError) -> dict[str, Any]:
    """FsError → 给模型的结构化错误（人话 + 稳定码）。"""
    return {"error": str(exc), "code": exc.code}


def _register_calculate(registry: ToolRegistry) -> None:
    registry.register(
        define_tool(
            "calculate",
            "计算一个算术表达式并返回精确结果。涉及算术时必须用本工具，不要心算。",
            schema_from_model(CalculateArgs),
        ),
        calculate,
    )


def _register_fs_tools(registry: ToolRegistry, fs: FileSystem) -> None:
    """read/write/list 三件套：实现只依赖 FileSystem 协议。"""

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
            files = fs.list_files(path)
        except FsError as exc:
            return _as_tool_error(exc)
        return {"path": path, "files": files}

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


def _register_edit_tool(registry: ToolRegistry, fs: FileSystem) -> None:
    """edit_file：精确局部替换（骑在 FileSystem 接缝的 edit_text 上）。

    语义由接缝保证（`_04` 定的）：old 恰好出现一次才替换；0 次 → FS_EDIT_NO_MATCH、
    多次 → FS_EDIT_AMBIGUOUS（拒绝猜）。**改文件比写文件更容易出错**——先读后改、
    精确匹配、拒绝歧义，正是编码代理最需要的防护。越界由 jail 策略拦（同 write）。
    """

    def edit_file(path: str, old: str, new: str) -> dict[str, Any]:
        try:
            fs.edit_text(path, old, new)
        except FsError as exc:
            return _as_tool_error(exc)
        return {"edited": path, "removed": len(old), "added": len(new)}

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
    """search_text / find_files：只读的检索工具（编码代理的"眼睛"）。

    实现只依赖 FileSystem 协议（list + read），因此 local / memory / jail 下
    行为一致：jail 的越界读被放行（策略只拦改动——检索不越权）。
    输出有上限（max_results），截断时在结果里如实说明——防止一次大搜索
    把工具结果和会话日志炸掉。
    """
    import fnmatch
    import re

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
                continue  # 列出后被删/是二进制等：跳过，不炸整个检索
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
            if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(file_path.split("/")[-1], pattern)
        ]
        truncated = len(matched) > max_results
        result: dict[str, Any] = {
            "pattern": pattern,
            "path": path,
            "files": matched[:max_results],
        }
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


def _bounded(text: str, cap: int) -> str:
    """输出上限：超长截断并附说明（防止单条事件把日志炸掉）。"""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…（输出过长已截断；原始长度 {len(text)} 字符）"


def _register_shell_tool(
    registry: ToolRegistry,
    subprocess_service: SubprocessService,
    *,
    workspace: Path | None,
    timeout: float,
    max_output_chars: int,
) -> None:
    """shell 工具：cwd/期限/输出上限在组装时解析，模型只给 command。"""
    cwd = str(workspace) if workspace is not None else None

    def shell(command: str) -> dict[str, Any]:
        try:
            result: CommandResult = subprocess_service.run(
                command, cwd=cwd, timeout=timeout
            )
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


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def build_toolbox(
    fs: FileSystem,
    subprocess_service: SubprocessService | None = None,
    *,
    workspace: str | Path | None = None,
    include_search: bool = False,
    shell_timeout: float = 30.0,
    shell_max_output_chars: int = 20_000,
) -> ToolRegistry:
    """组装会话的完整工具面。

    - fs：文件能力（必给；工具只认协议，传 LocalFS / MemoryFS / WorkspaceJailFS 均可）；
    - subprocess_service：命令执行能力（给了才注册 shell 工具）；
    - workspace：shell 的工作目录（显式解析点：跑之前把它定死，模型不可改）；
    - shell_timeout / shell_max_output_chars：期限与输出上限（同样是显式解析）。

    工具面（`_11` 起按"基本编码代理"补齐）：calculate / read_file / write_file /
    list_files / **edit_file / search_text / find_files** / shell（可选）/ web_search（可选）。
    """
    registry = ToolRegistry()
    _register_calculate(registry)
    _register_fs_tools(registry, fs)
    _register_edit_tool(registry, fs)      # `_11`：精确编辑
    _register_search_tools(registry, fs)   # `_11`：内容搜索 + 按名查找
    if subprocess_service is not None:
        _register_shell_tool(
            registry,
            subprocess_service,
            workspace=Path(workspace) if workspace is not None else None,
            timeout=shell_timeout,
            max_output_chars=shell_max_output_chars,
        )
    if include_search:
        from harness.tools.web_search import SearchArgs, make_web_search

        registry.register(
            define_tool(
                "web_search",
                "用关键词进行网络搜索，返回标题、链接和摘要。适合查询实时信息或不确定的事实时使用。",
                schema_from_model(SearchArgs),
            ),
            make_web_search(),
        )
    return registry


def build_filesystem_registry(fs: FileSystem, *, include_search: bool = False) -> ToolRegistry:
    """只要文件三件套的组装入口（`_04` 起保留；= build_toolbox 无 shell 版）。"""
    return build_toolbox(fs, None, include_search=include_search)


__all__ = [
    "ShellArgs",
    "EditFileArgs",
    "SearchTextArgs",
    "FindFilesArgs",
    "build_toolbox",
    "build_filesystem_registry",
]
