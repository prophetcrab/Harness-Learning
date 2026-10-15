"""tool-search 插件 —— 检索类工具（search_text / find_files）。

**边界**：只依赖 `fs` 服务；与 tool-fs **平级**（谁都不用谁的工具，只共享能力）。
这样拆的原因很实在：检索是"读很多文件"的重活，有时想关掉它省上下文、有时想
只在需要时装上；而读写是刚需。两者绑在一起就没法分开取舍了。

两件工具都**自带上限与截断说明**（结果超限时把"已截断、请收窄"写进返回值）：
给模型的错误/边界信息必须显式，否则它会以为"就这么多"。
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from components.fs import FileSystem, FsError
from components.tools import DeclarativeTool, tool
from kernel import Context
from plugins.services import FS_SERVICE, TOOLS_SERVICE


def _build(fs: FileSystem) -> tuple[DeclarativeTool, ...]:
    """按这一份 fs 造出两件检索工具（闭包绑定，理由见 tool_fs.py）。"""

    @tool("search_text")
    def search_text(pattern: str, path: str = ".", max_results: int = 50) -> dict[str, Any]:
        """在工作区文件内容里按正则搜索，返回 文件+行号+该行文本。

        找代码、找 TODO、定位位置时用它；比逐个 read_file 快。

        参数 pattern：要搜索的正则表达式，例如 'def .*\\(' 或 'TODO'（区分大小写）。
        参数 path：从工作区的哪个目录开始搜，默认整个工作区。
        参数 max_results：最多返回多少条匹配，默认 50。
        """
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return {"error": f"正则表达式不合法：{exc}", "code": "INVALID_PATTERN"}
        try:
            files = fs.list_files(path)
        except FsError as exc:
            return {"error": str(exc), "code": exc.code}

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

    @tool("find_files")
    def find_files(pattern: str, path: str = ".", max_results: int = 200) -> dict[str, Any]:
        """按文件名通配模式（如 '*.py'、'test_*.py'）找文件，返回相对路径列表。

        参数 pattern：文件名的通配模式（shell 风格），如 '*.py'、'notes/*'。
        参数 path：从工作区的哪个目录开始找，默认整个工作区。
        参数 max_results：最多返回多少个文件，默认 200。
        """
        try:
            files = fs.list_files(path)
        except FsError as exc:
            return {"error": str(exc), "code": exc.code}
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

    return (search_text, find_files)


class ToolSearchPlugin:
    """提供检索两件工具（inject fs + tools）。"""

    name = "tool-search"
    category = "capability"
    inject = (FS_SERVICE, TOOLS_SERVICE)

    def setup(self, ctx: Context):
        fs: FileSystem = ctx.require(FS_SERVICE)
        registry = ctx.require(TOOLS_SERVICE)

        built = _build(fs)
        for piece in built:
            registry.register(piece.definition, piece.func, owner=self.name)

        def dispose() -> None:
            for piece in built:
                registry.unregister(piece.name)

        return dispose


def tool_search_plugin() -> ToolSearchPlugin:
    return ToolSearchPlugin()


__all__ = ["ToolSearchPlugin", "tool_search_plugin"]
