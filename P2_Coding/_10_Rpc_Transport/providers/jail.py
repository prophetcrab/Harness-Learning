"""WorkspaceJailFS —— 策略型 provider：把"改动"关进工作区（越界一律拒绝）。

M5 的第二个机制（`_05` 唯一新增的东西）。与 LocalFS / MemoryFS 这类**机制型**
provider 的区别在于回答的问题不同：

    机制型 provider：文件**怎么**存（磁盘 / 内存 / …）
    策略型 provider：**允许**在哪里动（这个模块：只许在工作区内）

实现是**装饰器**：包住任意一个 FileSystem，在"改动"操作前做路径围栏，其余原样透传。
于是它不关心底层是磁盘还是内存——`WorkspaceJailFS(LocalFS(ws))` 是真实磁盘上的
围栏；`WorkspaceJailFS(MemoryFS())` 是内存上的围栏（测试证明策略与机制正交）。

三条边界（守住这三点，"围栏"才是对的）：

1. **只拦改动，不拦读**（write_text / edit_text 设卡；read_text / list_files 透传）。
   读不是破坏性动作——这正是 dsh `fs-sandbox` 的边界："confines model file
   mutations ... preserving the local filesystem's read behavior"。
2. **判据是词法解析后的相对路径**：`..` 逃逸、绝对路径（`/x`、`C:/x`、`\\server\\x`）
   一律拒绝；`notes/../a.txt` 这类"绕圈但仍在区内"的路径被规范化后放行
   （规范化结果也交给底层，保证各 provider 行为一致）。
3. **真实磁盘再加一道物理复核**：底层若暴露 root（如 LocalFS），把规范化路径
   拼回真实根再 `resolve()` 一次，仍须落在根内——挡住符号链接指向区外的绕行。
   内存 provider 没有真实根，词法检查就是全部。

拒绝时抛 `FsError(FS_SANDBOX_DENIED, ...)`（命名对齐 dsh）。工具层会把它翻译成
和其他文件错误同构的 `{"error": 人话, "code": "FS_SANDBOX_DENIED"}`——审批放行
也拦不住它（沙箱是"无论同不同意都不许"的第二道防线）。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from providers.filesystem import FS_SANDBOX_DENIED, FileSystem, FsError


def _resolve_inside(path: str) -> str:
    """把相对路径词法规范化为"区内形态"；逃逸/绝对路径 → FS_SANDBOX_DENIED。

    规则：先把反斜杠归一成 `/`（Windows 风格输入也要能被拦住），再逐段解析：
    `.` 丢弃、`..` 弹栈（弹空了即逃逸）、其余入栈。返回 `a/b` 形态的相对路径
    （根为 `.`）。纯词法计算，不碰文件系统——因此对任何 base 都适用。
    """
    text = str(path).replace("\\", "/")
    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise FsError(FS_SANDBOX_DENIED, f"绝对路径越出工作区，拒绝改动：{path}")
    parts = pure.parts
    if parts and ":" in parts[0]:
        # Windows 盘符形态（C:/x、C:x）——不是一个合法的工作区相对路径。
        raise FsError(FS_SANDBOX_DENIED, f"带盘符的路径越出工作区，拒绝改动：{path}")

    stack: list[str] = []
    for segment in parts:
        if segment == "..":
            if not stack:
                raise FsError(FS_SANDBOX_DENIED, f"路径越出工作区，拒绝改动：{path}")
            stack.pop()
        else:
            stack.append(segment)
    return "/".join(stack) if stack else "."


class WorkspaceJailFS:
    """包住任意 FileSystem：写入/编辑的路径必须落在工作区内（FileSystem 协议实现）。"""

    def __init__(self, base: FileSystem) -> None:
        self._base = base

    @property
    def base(self) -> FileSystem:
        """被围栏的底层 provider（只读）。"""
        return self._base

    @property
    def workspace(self) -> Path | None:
        """工作区根的真实路径（底层有 `root` 时给出，如 LocalFS；否则 None）。"""
        root = getattr(self._base, "root", None)
        return Path(root).resolve() if root is not None else None

    # ------------------------------------------------------------------
    # 内部：围栏
    # ------------------------------------------------------------------

    def _fence(self, path: str) -> str:
        """检查并规范化一个"要改动"的路径；越界 → FS_SANDBOX_DENIED。"""
        normalized = _resolve_inside(path)
        root = getattr(self._base, "root", None)
        if root is not None:
            # 物理复核：真实磁盘上 resolve 之后仍须落在根内（防符号链接绕行）。
            real_root = Path(root).resolve()
            target = (real_root / normalized).resolve()
            if not target.is_relative_to(real_root):
                raise FsError(
                    FS_SANDBOX_DENIED,
                    f"路径解析后越出工作区（链接指向区外？），拒绝改动：{path}",
                )
        return normalized

    # ------------------------------------------------------------------
    # FileSystem 协议：改动设卡 …
    # ------------------------------------------------------------------

    def write_text(self, path: str, content: str) -> None:
        self._base.write_text(self._fence(path), content)

    def edit_text(self, path: str, old: str, new: str) -> None:
        self._base.edit_text(self._fence(path), old, new)

    # ------------------------------------------------------------------
    # … 其余原样透传（只拦改动，不拦读——见模块头"三条边界"）
    # ------------------------------------------------------------------

    def read_text(self, path: str) -> str:
        return self._base.read_text(path)

    def list_files(self, path: str = ".") -> list[str]:
        return self._base.list_files(path)


__all__ = ["WorkspaceJailFS"]
