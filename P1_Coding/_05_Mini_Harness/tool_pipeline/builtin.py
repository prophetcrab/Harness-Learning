"""内置工具 —— 注册表 + 管线开箱可用的几个工具。

选这几个是为了覆盖管线要演示的每条路径：
- `calculate`  正常成功（从 _01 沿用安全计算器；参数用 pydantic 声明 schema）
- `sleep`      长耗时 → 演示超时（execute 段）
- `echo_long`  返回超长字符串 → 演示 post 段截断
- `write_file` 写工作区文件 → 演示 needs_approval + 工作区沙箱

`write_file` 带了和 P0 `_04` 相同的工作区沙箱：路径 resolve 后必须落在
workspace 目录内，越界直接拒绝（不靠审批兜底 —— 审批是"人同意"，沙箱是
"无论同不同意都不许"，两道防线各管各的）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from llm_seam.tools import calculate  # 复用 _01 的安全计算器实现
from tool_pipeline.registry import ToolDefinition, ToolRegistry, define_tool
from tool_pipeline.schema import schema_from_model


class CalculateArgs(BaseModel):
    expression: str = Field(
        description="要计算的算术表达式，例如 '1234*56.78'。支持 + - * / // % ** 和括号。"
    )


class SleepArgs(BaseModel):
    seconds: float = Field(description="睡眠秒数（用于演示超时）", ge=0)


class EchoLongArgs(BaseModel):
    repeat: int = Field(default=50, description="重复 '长文本' 的次数（演示 post 截断）")


class WriteFileArgs(BaseModel):
    path: str = Field(description="相对工作区根目录的文件路径，例如 'notes/todo.txt'")
    content: str = Field(description="要写入的文本内容")


def _sleep(seconds: float) -> dict:
    import time

    time.sleep(seconds)
    return {"slept": seconds}


def _echo_long(repeat: int) -> dict:
    return {"text": "长文本" * repeat}


def make_write_file(workspace: Path):
    """构造一个写工作区文件的工具实现（带沙箱，越界拒绝）。"""
    root = workspace.resolve()

    def write_file(path: str, content: str) -> dict:
        target = (root / path).resolve()
        # 铁律：写权限必须限定在工作区内。is_relative_to 挡住 '../' 穿越。
        if not target.is_relative_to(root):
            return {"error": f"路径越出工作区，拒绝写入：{path}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"written": str(target.relative_to(root)), "bytes": len(content.encode("utf-8"))}

    return write_file


def build_default_registry(workspace: Path | None = None) -> ToolRegistry:
    """组装本练习默认的工具注册表。

    workspace 给出时额外注册 write_file（needs_approval=True）；
    不给出时只有只读/无害工具，适合不需要审批的演示。
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
    registry.register(
        define_tool("sleep", "睡眠指定秒数（仅用于演示执行超时）。", schema_from_model(SleepArgs)),
        _sleep,
    )
    registry.register(
        define_tool(
            "echo_long",
            "返回一段重复文本（仅用于演示 post 阶段截断超长输出）。",
            schema_from_model(EchoLongArgs),
        ),
        _echo_long,
    )

    if workspace is not None:
        registry.register(
            define_tool(
                "write_file",
                "把文本写入工作区内的文件（写入前需要人工审批）。",
                schema_from_model(WriteFileArgs),
                needs_approval=True,
            ),
            make_write_file(workspace),
        )

    return registry


__all__ = [
    "CalculateArgs",
    "SleepArgs",
    "EchoLongArgs",
    "WriteFileArgs",
    "make_write_file",
    "build_default_registry",
    "ToolDefinition",
]
