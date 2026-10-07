"""工作区工具箱 —— mini harness 的默认工具面：计算器 + 文件读写 + 可选搜索。

把 P0 各个单文件脚本里的工具，收敛成一份"harness 级"工具箱，注册进 _03 的
`ToolRegistry`（定义与实现分离），由 `ToolPipeline` 统一执行：

    calculate   安全计算器（AST 白名单，复用 _01 的实现）
    read_file   读工作区内文件
    write_file  写工作区内文件（needs_approval=True —— 先过审批）
    list_files  列工作区内文件
    web_search  可选（--search 才注册）：联网搜索，默认关闭以保持离线

两条独立防线（与 _03 一致）：
- 审批是"人同意"：write_file 标了 needs_approval，管线会转 ask；
- 沙箱是"无论同不同意都不许"：所有文件工具路径 resolve 后必须落在工作区内，
  越界（如 '../x'）直接拒绝，审批放行也挡。

注册进注册表的东西是"定义 + 实现"；给模型看的只是定义产出的 spec()。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.llm.tools import calculate
from harness.tools.registry import ToolRegistry, define_tool
from harness.tools.schema import schema_from_model

# ---------------------------------------------------------------------------
# 参数模型（pydantic → JSON Schema）
# ---------------------------------------------------------------------------


class CalculateArgs(BaseModel):
    expression: str = Field(
        description="要计算的算术表达式，例如 '1234*56.78'。支持 + - * / // % ** 和括号。"
    )


class ReadFileArgs(BaseModel):
    path: str = Field(description="相对工作区根目录的文件路径，例如 'notes/todo.txt'")


class WriteFileArgs(BaseModel):
    path: str = Field(description="相对工作区根目录的文件路径，例如 'notes/todo.txt'")
    content: str = Field(description="要写入的文本内容")


class ListFilesArgs(BaseModel):
    path: str = Field(default=".", description="相对工作区根目录的目录路径，默认为工作区根")


# ---------------------------------------------------------------------------
# 实现（带工作区沙箱）
# ---------------------------------------------------------------------------


def _resolve_in_workspace(root: Path, path: str) -> tuple[Path | None, dict[str, Any] | None]:
    """把相对路径解析到工作区内；越界时返回 (None, 错误)。

    铁律：写/读权限必须限定在工作区内。is_relative_to 挡住 '../' 穿越与绝对路径。
    """
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return None, {"error": f"路径越出工作区，拒绝访问：{path}"}
    return target, None


def make_file_tools(
    workspace: Path,
) -> tuple[Callable[..., dict[str, Any]], Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]:
    """构造 read_file / write_file / list_files 三个实现（共享工作区沙箱）。"""
    root = workspace.resolve()

    def read_file(path: str) -> dict[str, Any]:
        target, error = _resolve_in_workspace(root, path)
        if error is not None:
            return error
        assert target is not None
        if not target.is_file():
            return {"error": f"文件不存在：{path}"}
        return {"path": path, "content": target.read_text(encoding="utf-8")}

    def write_file(path: str, content: str) -> dict[str, Any]:
        target, error = _resolve_in_workspace(root, path)
        if error is not None:
            return error
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"written": str(target.relative_to(root)), "bytes": len(content.encode("utf-8"))}

    def list_files(path: str = ".") -> dict[str, Any]:
        target, error = _resolve_in_workspace(root, path)
        if error is not None:
            return error
        assert target is not None
        if not target.is_dir():
            return {"error": f"目录不存在：{path}"}
        files = sorted(str(p.relative_to(root)) for p in target.rglob("*") if p.is_file())
        return {"path": path, "files": files}

    return read_file, write_file, list_files


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def build_workspace_registry(workspace: Path, *, include_search: bool = False) -> ToolRegistry:
    """组装 mini harness 的默认工具注册表。

    workspace 是文件工具的沙箱根；include_search=True 时额外注册 web_search
    （默认关闭，避免测试/演示依赖网络）。
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

    read_file, write_file, list_files = make_file_tools(workspace)
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


__all__ = [
    "CalculateArgs",
    "ReadFileArgs",
    "WriteFileArgs",
    "ListFilesArgs",
    "make_file_tools",
    "build_workspace_registry",
]
