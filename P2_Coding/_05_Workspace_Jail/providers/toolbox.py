"""工具箱（Consumer 角色）—— 文件工具只依赖 FileSystem 抽象。

这是接缝的第三个角色：`read_file` / `write_file` / `list_files` 三个工具的
**实现**不再直接碰 pathlib，而是调用注入进来的 `FileSystem`（协议），
于是同一套工具在 local / memory（以及将来任何 provider）下行为一致。

错误翻译（唯一一处接缝词汇 → 工具词汇）：provider 抛的 `FsError` 在这里被
转成模型友好的结构化结果 `{"error": <人话>, "code": <稳定码>}` —— 铁律 #7
"错误是给模型的输入"：工具失败不炸循环，而是让模型拿到可理解、可自救的信息。

工具的定义（名字/说明/schema）与基线保持一致（复用 `harness.tools.workspace`
里的参数模型），只有实现被接缝化——"换实现不改上层"，这里改的就是实现。
calculate 不是文件能力，沿用在基线的纯函数实现，一并注册保持工具面完整。
"""

from __future__ import annotations

from typing import Any

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


def _as_tool_error(exc: FsError) -> dict[str, Any]:
    """FsError → 给模型的结构化错误（人话 + 稳定码）。"""
    return {"error": str(exc), "code": exc.code}


def build_filesystem_registry(
    fs: FileSystem, *, include_search: bool = False
) -> ToolRegistry:
    """按一份 FileSystem 实现组装工具注册表（calculate + 文件三件套 + 可选搜索）。

    与基线 `build_workspace_registry` 的工具面一致，区别只在文件工具
    通过注入的 provider 干活——传 LocalFS 就是磁盘、传 MemoryFS 就是内存。
    """
    registry = ToolRegistry()

    registry.register(
        define_tool(
            "calculate",
            "计算一个算术表达式并返回精确结果。涉及算术时必须用本工具，不要心算。",
            schema_from_model(CalculateArgs),
        ),
        calculate,
    )

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


__all__ = ["build_filesystem_registry"]
