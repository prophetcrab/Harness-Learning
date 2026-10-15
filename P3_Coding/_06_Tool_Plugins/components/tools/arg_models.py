"""工具参数模型 —— pydantic 模型 → JSON Schema（给模型看的参数说明）。

汇总自两处（原样沿用）：
- `harness/tools/workspace.py`：calculate / read_file / write_file / list_files；
- `providers/toolbox.py`（P2 `_11`）：edit_file / shell / search_text / find_files。

字段描述就是"给模型看的说明书"的一部分——schema_from_model() 会把它
变成工具定义里的 parameters。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class EditFileArgs(BaseModel):
    path: str = Field(description="相对工作区根目录的文件路径，例如 'src/app.py'")
    old: str = Field(
        description=(
            "要被替换的原文（必须与文件中的文本逐字符一致，且在文件里**恰好出现一次**）。"
            "出现零次或多次都会报错——这是防误改的保护。"
        )
    )
    new: str = Field(description="替换后的新文本（可以为空字符串，表示删除 old）")


class ShellArgs(BaseModel):
    command: str = Field(
        description=(
            "要在工作目录下执行的命令行（经系统 shell 解释，例如 'dir' 或 "
            "'python -c \"print(1+1)\"'）。工作目录与会话工作区一致，不能指定其它目录。"
        )
    )


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


__all__ = [
    "CalculateArgs",
    "EditFileArgs",
    "FindFilesArgs",
    "ListFilesArgs",
    "ReadFileArgs",
    "SearchTextArgs",
    "ShellArgs",
    "WriteFileArgs",
]
