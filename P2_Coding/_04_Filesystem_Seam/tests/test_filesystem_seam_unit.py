"""_04_Filesystem_Seam 的单元测试：接缝契约 / 双 provider 对照 / 单槽服务。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（providers/ 包：FileSystem 定义、LocalFS、MemoryFS、ServiceContainer），
以及它们与工具层的契约。

覆盖：
1. 契约套件：同一组断言在 local 与 memory 两个 provider 上各跑一遍
   （参数化 fixture —— "换实现不改测试"的验收形状）
2. 错误词汇：稳定码（FS_NOT_FOUND / FS_NOT_A_FILE / FS_EDIT_NO_MATCH /
   FS_EDIT_AMBIGUOUS），两个 provider 行为一致
3. edit 语义：恰好一次才替换；0 次/多次都拒绝（不猜）
4. 单槽服务：重复注册报错 / 未注册 resolve 报错 / has / capabilities
5. 工具层：FsError → {"error", "code"} 结构化结果（铁律 #7）

运行：cd P2_Coding/_04_Filesystem_Seam && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from providers import (
    FS_CAPABILITY,
    FS_EDIT_AMBIGUOUS,
    FS_EDIT_NO_MATCH,
    FS_NOT_A_DIR,
    FS_NOT_A_FILE,
    FS_NOT_FOUND,
    FileSystem,
    FsError,
    LocalFS,
    MemoryFS,
    ServiceContainer,
    build_filesystem_registry,
)


@pytest.fixture(params=["local", "memory"])
def fs(request, tmp_path: Path) -> FileSystem:
    """★ 契约套件：同一套断言，两种 provider 各跑一遍。"""
    if request.param == "local":
        return LocalFS(tmp_path / "ws")
    return MemoryFS()


# =========================================================================
# 1) 契约套件（参数化双 provider）
# =========================================================================


def test_protocol_is_satisfied(fs: FileSystem):
    """两个实现都满足 FileSystem 协议（runtime_checkable 的结构检查）。"""
    assert isinstance(fs, FileSystem)


def test_write_then_read_round_trip(fs: FileSystem):
    fs.write_text("notes/a.txt", "hello")
    assert fs.read_text("notes/a.txt") == "hello"


def test_write_overwrites_whole_file(fs: FileSystem):
    fs.write_text("a.txt", "第一版")
    fs.write_text("a.txt", "第二版")
    assert fs.read_text("a.txt") == "第二版"


def test_write_creates_parent_directories(fs: FileSystem):
    fs.write_text("deep/nested/dir/file.txt", "x")
    assert fs.read_text("deep/nested/dir/file.txt") == "x"


def test_read_missing_file_is_not_found(fs: FileSystem):
    with pytest.raises(FsError) as info:
        fs.read_text("missing.txt")
    assert info.value.code == FS_NOT_FOUND


def test_read_directory_is_not_a_file(fs: FileSystem):
    fs.write_text("notes/a.txt", "x")
    with pytest.raises(FsError) as info:
        fs.read_text("notes")
    assert info.value.code == FS_NOT_A_FILE


def test_list_files_is_recursive_and_sorted(fs: FileSystem):
    fs.write_text("notes/b.txt", "b")
    fs.write_text("notes/a.txt", "a")
    fs.write_text("root.txt", "r")
    assert fs.list_files(".") == ["notes/a.txt", "notes/b.txt", "root.txt"]
    assert fs.list_files("notes") == ["notes/a.txt", "notes/b.txt"]


def test_list_missing_directory_is_not_found(fs: FileSystem):
    with pytest.raises(FsError) as info:
        fs.list_files("nope")
    assert info.value.code == FS_NOT_FOUND


def test_list_file_is_not_a_dir(fs: FileSystem):
    fs.write_text("a.txt", "x")
    with pytest.raises(FsError) as info:
        fs.list_files("a.txt")
    assert info.value.code == FS_NOT_A_DIR


def test_edit_replaces_exactly_once(fs: FileSystem):
    fs.write_text("a.txt", "hello world")
    fs.edit_text("a.txt", "world", "harness")
    assert fs.read_text("a.txt") == "hello harness"


def test_edit_no_match_is_loud(fs: FileSystem):
    fs.write_text("a.txt", "hello")
    with pytest.raises(FsError) as info:
        fs.edit_text("a.txt", "world", "x")
    assert info.value.code == FS_EDIT_NO_MATCH


def test_edit_ambiguous_is_loud(fs: FileSystem):
    """待替换文本出现多次 → 拒绝猜测（原子编辑不猜）。"""
    fs.write_text("a.txt", "x x x")
    with pytest.raises(FsError) as info:
        fs.edit_text("a.txt", "x", "y")
    assert info.value.code == FS_EDIT_AMBIGUOUS
    assert fs.read_text("a.txt") == "x x x"  # 拒绝时文件保持原样


def test_error_message_is_human_readable(fs: FileSystem):
    """FsError 的 str() 是人话（给模型看），code 是稳定码（给程序看）。"""
    with pytest.raises(FsError) as info:
        fs.read_text("missing.txt")
    assert "missing.txt" in str(info.value)
    assert info.value.code == FS_NOT_FOUND


# =========================================================================
# 2) 实现间的差异（各自的"本性"）
# =========================================================================


def test_memory_fs_touches_no_disk(tmp_path: Path):
    """MemoryFS 的写操作完全留在内存：磁盘根目录全程不存在。"""
    root = tmp_path / "never_created"
    fs = MemoryFS()
    fs.write_text("notes/a.txt", "x")
    assert fs.read_text("notes/a.txt") == "x"
    assert not root.exists()


def test_local_fs_writes_real_files(tmp_path: Path):
    """LocalFS 的写操作真的落盘（用 pathlib 从外部验证，证明不是内存假象）。"""
    root = tmp_path / "ws"
    fs = LocalFS(root)
    fs.write_text("notes/a.txt", "落盘")
    assert (root / "notes" / "a.txt").read_text(encoding="utf-8") == "落盘"


def test_two_memory_fs_are_isolated():
    """两个 MemoryFS 互不相干（各自的内存命名空间）。"""
    a, b = MemoryFS(), MemoryFS()
    a.write_text("x.txt", "a")
    with pytest.raises(FsError):
        b.read_text("x.txt")


def test_memory_fs_normalizes_paths():
    """MemoryFS 规范化路径：./、重复斜杠折叠一致。"""
    fs = MemoryFS()
    fs.write_text("./notes//a.txt", "x")
    assert fs.read_text("notes/a.txt") == "x"
    assert fs.list_files(".") == ["notes/a.txt"]


# =========================================================================
# 3) 单槽服务：fail loud 的注册与解析（铁律 #5 / #6）
# =========================================================================


def test_duplicate_registration_is_loud():
    services = ServiceContainer()
    services.register(FS_CAPABILITY, MemoryFS())
    with pytest.raises(ValueError, match="已注册"):
        services.register(FS_CAPABILITY, LocalFS("ws"))


def test_resolve_unregistered_is_loud():
    services = ServiceContainer()
    with pytest.raises(KeyError, match="未注册"):
        services.resolve("llm")


def test_resolve_returns_the_registered_provider():
    services = ServiceContainer()
    provider = MemoryFS()
    services.register(FS_CAPABILITY, provider)
    assert services.resolve(FS_CAPABILITY) is provider


def test_has_and_capabilities():
    services = ServiceContainer()
    assert services.has(FS_CAPABILITY) is False
    services.register(FS_CAPABILITY, MemoryFS())
    assert services.has(FS_CAPABILITY) is True
    assert services.capabilities == [FS_CAPABILITY]


# =========================================================================
# 4) 工具层：消费者只认协议；错误翻译成给模型的结构化结果
# =========================================================================


@pytest.fixture(params=["local", "memory"])
def tool_registry(request, tmp_path: Path):
    """工具注册表也跑双 provider：工具行为与实现无关。"""
    fs = LocalFS(tmp_path / "ws") if request.param == "local" else MemoryFS()
    return build_filesystem_registry(fs), request.param


def _call(registry, name: str, **arguments):
    return registry.resolve(name).func(**arguments)


def test_tools_write_read_list(tool_registry):
    registry, _ = tool_registry
    assert _call(registry, "write_file", path="notes/a.txt", content="data") == {
        "written": "notes/a.txt",
        "bytes": 4,
    }
    assert _call(registry, "read_file", path="notes/a.txt") == {
        "path": "notes/a.txt",
        "content": "data",
    }
    assert _call(registry, "list_files", path=".") == {
        "path": ".",
        "files": ["notes/a.txt"],
    }


def test_tools_translate_fs_error(tool_registry):
    """FsError → {"error": 人话, "code": 稳定码}：模型拿到可自救的结构化错误。"""
    registry, _ = tool_registry
    result = _call(registry, "read_file", path="none.txt")
    assert result["code"] == FS_NOT_FOUND
    assert "none.txt" in result["error"]

    result = _call(registry, "list_files", path="none")
    assert result["code"] == FS_NOT_FOUND


def test_tools_have_same_surface_as_baseline(tool_registry):
    """工具面与基线一致：calculate + 文件三件套；write_file 仍需审批。"""
    registry, _ = tool_registry
    assert registry.names == ["calculate", "read_file", "write_file", "list_files"]
    assert registry.resolve("write_file").definition.needs_approval is True
