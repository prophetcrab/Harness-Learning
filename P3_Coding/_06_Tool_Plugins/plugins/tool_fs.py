"""tool-fs 插件 —— 文件读写类工具（read / write / list / edit）。

**边界**：只依赖 `fs` 服务（文件系统能力）。它不认识真实磁盘、也不认识别的工具
插件——想换沙箱（jail / 内存 / 真实磁盘）就换 fs 插件，本插件一行不用改。
这是"工具只调用协议"的既有纪律，`_06` 只是把它落成**一个独立插件**。

**危险工具照旧走审批**：write_file / edit_file 标 `needs_approval`，由管线在 pre
阶段问策略（`_01` 起的语义，未变）。

**与 tool-search 的关系**：两者都注入 fs，那是**共享能力**而不是互相依赖——
search 不调用 read_file 这件工具，所以两件工具插件可以各自装卸、互不牵连。

**工具函数是"每次 setup 现造"的**（`_build(fs)` 里定义）：fs 服务是装配时才
存在的，把工具函数定义在工厂里、用闭包抓住那一次装配的 fs，工具就永远认准
自己那一份——两个 harness 同时活在一个进程里（测试就是这么干的）也不会串。
模块级声明 + 一个"当前 fs"全局是更短，但那是错的：后装配的一方会覆盖先装配
的一方的 fs，前者的工具就悄悄指向了别人的工作区。
"""

from __future__ import annotations

from typing import Any

from components.fs import FileSystem, FsError
from components.tools import DeclarativeTool, tool
from kernel import Context
from plugins.services import FS_SERVICE, TOOLS_SERVICE


def _as_tool_error(exc: FsError) -> dict[str, Any]:
    """FsError → 给模型的结构化错误（人话 + 稳定码）。"""
    return {"error": str(exc), "code": exc.code}


def _build(fs: FileSystem) -> tuple[DeclarativeTool, ...]:
    """按这一份 fs 造出四件工具（闭包绑定；见模块说明）。"""

    @tool("read_file")
    def read_file(path: str) -> dict[str, Any]:
        """读取工作区内某个文件的文本内容。路径是相对工作区根目录的相对路径。

        参数 path：相对工作区根目录的文件路径，例如 'notes/todo.txt'。
        """
        try:
            return {"path": path, "content": fs.read_text(path)}
        except FsError as exc:
            return _as_tool_error(exc)

    @tool("write_file", needs_approval=True)
    def write_file(path: str, content: str) -> dict[str, Any]:
        """把文本写入工作区内的文件（会创建父目录）。写入前需要人工审批。

        参数 path：相对工作区根目录的文件路径，例如 'notes/todo.txt'。
        参数 content：要写入的文本内容。
        """
        try:
            fs.write_text(path, content)
        except FsError as exc:
            return _as_tool_error(exc)
        return {"written": path, "bytes": len(content.encode("utf-8"))}

    @tool("list_files")
    def list_files(path: str = ".") -> dict[str, Any]:
        """列出工作区内某个目录下的所有文件（相对路径）。

        参数 path：相对工作区根目录的目录路径，默认为工作区根。
        """
        try:
            return {"path": path, "files": fs.list_files(path)}
        except FsError as exc:
            return _as_tool_error(exc)

    @tool("edit_file", needs_approval=True)
    def edit_file(path: str, old: str, new: str) -> dict[str, Any]:
        """把工作区内文件里的一段原文替换成新文本。改文件优先用它，而不是整文件重写。

        参数 path：相对工作区根目录的文件路径，例如 'src/app.py'。
        参数 old：要被替换的原文（必须与文件中的文本逐字符一致，且在文件里**恰好
        出现一次**；出现零次或多次都会报错——这是防误改的保护）。
        参数 new：替换后的新文本（可以为空字符串，表示删除 old）。
        """
        try:
            fs.edit_text(path, old, new)
        except FsError as exc:
            return _as_tool_error(exc)
        return {"edited": path, "removed": len(old), "added": len(new)}

    return (read_file, write_file, list_files, edit_file)


class ToolFsPlugin:
    """提供文件读写四件工具（inject fs + tools）。"""

    name = "tool-fs"
    #: 应用角色（capability）：可按需装卸的一件能力。
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


def tool_fs_plugin() -> ToolFsPlugin:
    """工厂（`plugins/catalog.py` 的白名单里用的是它）。"""
    return ToolFsPlugin()


__all__ = ["ToolFsPlugin", "tool_fs_plugin"]
