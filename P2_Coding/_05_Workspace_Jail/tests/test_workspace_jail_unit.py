"""_05_Workspace_Jail 的单元测试：策略型 provider 的围栏语义。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（providers/jail.py 的 WorkspaceJailFS），以及它与 `_04` 接缝定义的契约。

覆盖：
1. 围栏：越界写/编辑被拒（多种路径形态：`..`、绝对路径、盘符、反斜杠）；
   平衡的 `..`（绕圈但仍在区内）放行并规范化
2. 只拦改动：读与列目录原样透传（包括区外文件）
3. 策略与机制正交：同一道围栏叠在 LocalFS / MemoryFS 上行为一致
4. 错误词汇：FS_SANDBOX_DENIED 与其它文件错误同构（FsError + 稳定码）
5. 契约：WorkspaceJailFS 满足 FileSystem 协议；base/workspace 属性

运行：cd P2_Coding/_05_Workspace_Jail && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from providers import (
    FS_SANDBOX_DENIED,
    FileSystem,
    FsError,
    LocalFS,
    MemoryFS,
    WorkspaceJailFS,
)


@pytest.fixture(params=["local", "memory"])
def jail(request, tmp_path: Path) -> WorkspaceJailFS:
    """★ 围栏叠在两种机制上：同一套断言各跑一遍（策略与机制正交）。"""
    if request.param == "local":
        return WorkspaceJailFS(LocalFS(tmp_path / "ws"))
    return WorkspaceJailFS(MemoryFS())


def _write_outside(jail: WorkspaceJailFS, path: str) -> None:
    jail.write_text(path, "x")


# =========================================================================
# 1) 围栏：越界一律拒绝
# =========================================================================


def test_protocol_is_satisfied(jail: WorkspaceJailFS):
    assert isinstance(jail, FileSystem)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.txt",             # 直接上一级
        "..\\escape.txt",            # 反斜杠形态
        "notes/../../escape.txt",    # 中途弹栈弹空
        "a/b/c/../../../../x.txt",   # 弹得比栈深
        "/etc/passwd",               # 绝对路径
        "C:/evil.txt",               # 盘符绝对路径
        "C:evil.txt",                # 盘符相对路径
    ],
)
def test_write_escape_is_denied(jail: WorkspaceJailFS, bad_path: str):
    with pytest.raises(FsError) as info:
        _write_outside(jail, bad_path)
    assert info.value.code == FS_SANDBOX_DENIED
    assert "工作区" in str(info.value)


@pytest.mark.parametrize("bad_path", ["../escape.txt", "/tmp/x.txt"])
def test_edit_escape_is_denied(jail: WorkspaceJailFS, bad_path: str):
    with pytest.raises(FsError) as info:
        jail.edit_text(bad_path, "a", "b")
    assert info.value.code == FS_SANDBOX_DENIED


def test_inside_write_is_allowed(jail: WorkspaceJailFS):
    jail.write_text("notes/a.txt", "hello")
    assert jail.read_text("notes/a.txt") == "hello"


def test_balanced_dotdot_is_allowed_and_normalized(jail: WorkspaceJailFS):
    """绕圈但仍在区内（notes/../a.txt）→ 规范化后放行，落点与直写相同。"""
    jail.write_text("notes/../a.txt", "绕圈")
    assert jail.read_text("a.txt") == "绕圈"
    assert jail.list_files(".") == ["a.txt"]


def test_edit_inside_is_allowed(jail: WorkspaceJailFS):
    jail.write_text("a.txt", "hello world")
    jail.edit_text("a.txt", "world", "jail")
    assert jail.read_text("a.txt") == "hello jail"


def test_denied_write_leaves_no_file(jail: WorkspaceJailFS, tmp_path: Path):
    with pytest.raises(FsError):
        _write_outside(jail, "../escaped.txt")
    workspace = jail.workspace  # LocalFS 时给真实根；MemoryFS 时为 None
    assert workspace is None or not (workspace.parent / "escaped.txt").exists()


# =========================================================================
# 2) 只拦改动：读/列目录透传（区外可读是刻意的）
# =========================================================================


def test_read_outside_is_passthrough_for_local(tmp_path: Path):
    """LocalFS 上的围栏：读操作不设卡——区外文件读得到（与 dsh fs-sandbox 同边界）。"""
    root = tmp_path / "ws"
    outside = tmp_path / "outside.txt"
    outside.write_text("区外内容", encoding="utf-8")
    jail = WorkspaceJailFS(LocalFS(root))
    assert jail.read_text("../outside.txt") == "区外内容"


def test_read_outside_is_passthrough_for_memory():
    """MemoryFS 上的围栏同样只拦改动：读穿透到底层（区外读不到是底层没有，不是围栏拦）。"""
    base = MemoryFS()
    base.write_text("outside.txt", "内存区外")  # 围栏外面直接写
    jail = WorkspaceJailFS(base)
    assert jail.read_text("outside.txt") == "内存区外"
    # 而经围栏的改动不能"逃"到它外面（这里同路径是区内，验证透传与设卡的分界都工作）
    jail.write_text("inside.txt", "x")
    assert sorted(base.list_files(".")) == ["inside.txt", "outside.txt"]


def test_list_files_is_passthrough(jail: WorkspaceJailFS):
    jail.write_text("a.txt", "1")
    jail.write_text("sub/b.txt", "2")
    assert jail.list_files(".") == ["a.txt", "sub/b.txt"]


# =========================================================================
# 3) 策略与机制正交：同一道围栏，两种底座
# =========================================================================


def test_jail_does_not_care_about_base():
    """同一套越界断言在两种底座上结果一致（由参数化 fixture 覆盖）；这里钉住类型关系。"""
    local_jail = WorkspaceJailFS(LocalFS("ws"))
    memory_jail = WorkspaceJailFS(MemoryFS())
    for jail in (local_jail, memory_jail):
        with pytest.raises(FsError) as info:
            jail.write_text("../x.txt", "x")
        assert info.value.code == FS_SANDBOX_DENIED


def test_base_property_exposes_wrapped_provider():
    base = MemoryFS()
    jail = WorkspaceJailFS(base)
    assert jail.base is base


def test_workspace_property(tmp_path: Path):
    assert WorkspaceJailFS(MemoryFS()).workspace is None  # 无真实根
    root = tmp_path / "ws"
    assert WorkspaceJailFS(LocalFS(root)).workspace == root.resolve()


def test_local_denied_write_creates_no_workspace_dir(tmp_path: Path):
    """越界拒绝发生在任何落盘动作之前：连工作区目录都不会被建出来。"""
    root = tmp_path / "never"
    jail = WorkspaceJailFS(LocalFS(root))
    with pytest.raises(FsError):
        jail.write_text("../x.txt", "x")
    assert not root.exists()


def test_symlink_escape_is_denied(tmp_path: Path):
    """物理复核：工作区里的符号链接指向区外时，经它写也必须被拦住。

    （Windows 上建符号链接需要权限，建不了就跳过——词法检查仍挡住大部分形态。）
    """
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本平台不允许创建符号链接（权限不足），跳过物理复核用例")
    jail = WorkspaceJailFS(LocalFS(root))
    with pytest.raises(FsError) as info:
        jail.write_text("link/escaped.txt", "x")
    assert info.value.code == FS_SANDBOX_DENIED


# =========================================================================
# 4) 错误词汇：同构（与其它文件错误同一种异常、同一个翻译路径）
# =========================================================================


def test_denied_error_shape_matches_other_fs_errors(jail: WorkspaceJailFS):
    """越界错误与"文件不存在"是同一类异常（FsError）——调用方按 code 分支即可。"""
    with pytest.raises(FsError) as denied:
        jail.write_text("../x.txt", "x")
    with pytest.raises(FsError) as missing:
        jail.read_text("none.txt")
    assert denied.value.code == FS_SANDBOX_DENIED
    assert missing.value.code == "FS_NOT_FOUND"
    assert type(denied.value) is type(missing.value)  # 同一种异常类型


def test_denied_error_writes_nothing_via_edit(jail: WorkspaceJailFS):
    """越界 edit 不会先读后拒（不会产生半程副作用）——直接拒绝。"""
    with pytest.raises(FsError) as info:
        jail.edit_text("../outside.txt", "old", "new")
    assert info.value.code == FS_SANDBOX_DENIED
