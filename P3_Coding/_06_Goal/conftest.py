"""P3 基础包的 pytest 引导：把本目录置于导入搜索第一顺位。

本目录的顶层包/模块名（`kernel` / `components` / `plugins` / `chat` / `demo`）中，
`kernel` 与 P2 `_07`–`_11` 的各阶段目录**同名**，`chat` / `demo` 更是每个阶段都有。
全量运行（从项目根一次跑完 P0–P3）时，先被导入的那份会固化进 `sys.modules`，
导致后面的阶段 import 到错误版本。

对策与 P2 各阶段一致：在"开始收集本目录的测试模块前"和"运行本目录的测试前"
各激活一次——把本目录置于 sys.path[0]，并驱逐同名顶层包/模块的缓存。
单跑本包时这些钩子无副作用。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 需要隔离的同名顶层包/模块（本包新增 + 与 P2 阶段重名的入口模块）。
SHARED_NAMES = (
    "kernel",
    "components",
    "plugins",
    "chat",
    "demo",
    "harness",
    "prompt",
    "context",
    "providers",
    "config",
    "server",
    "webui",
)


def _activate() -> None:
    """把本目录置于导入搜索第一顺位，并清空同名顶层包/模块的缓存。"""
    here = str(HERE)
    if here in sys.path:
        sys.path.remove(here)
    sys.path.insert(0, here)
    for key in [k for k in sys.modules if k.split(".")[0] in SHARED_NAMES]:
        sys.modules.pop(key, None)


def _belongs(path) -> bool:
    if path is None:
        return False
    try:
        resolved = Path(str(path)).resolve()
    except (OSError, ValueError):
        return False
    return resolved == HERE or HERE in resolved.parents


def pytest_collectstart(collector) -> None:
    """开始收集某个节点前：属于本目录的节点先激活本目录的导入环境。"""
    if _belongs(getattr(collector, "path", None)):
        _activate()


def pytest_runtest_setup(item) -> None:
    """运行本目录的测试前再激活一次（覆盖函数内的延迟 import 与跨阶段缓存）。"""
    if _belongs(getattr(item, "path", None)):
        _activate()


_activate()
