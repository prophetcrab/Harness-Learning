"""_01_Prompt_Sections 的 pytest 引导（同名顶层包/模块的隔离）。

本阶段有若干与相邻阶段**同名**的顶层包/模块：`harness`（基线副本）、`prompt`
（本阶段新增，与 _02 重名）、`chat`（入口，与 _02 重名）。

pytest 在"多阶段同进程"运行（如 `pytest P2_Coding/_01_... P2_Coding/_02_...`
或从项目根一次跑完）时会有两个串味来源：
1. 各阶段目录的 conftest 会被**先全部预加载**，后加载者把自己的目录插到
   sys.path[0]——于是先收集的阶段的测试 import 同名包时可能命中后一阶段的副本；
2. 同名模块（如 `chat`）被第一个导入的阶段缓存进 sys.modules 后，后一阶段的
   `from chat import ...` 会直接拿到旧版本。

所以隔离不能只在 conftest 加载时做一次，而要在"开始收集本目录的测试模块前"和
"运行本目录的测试前"各激活一次：把本目录置于 sys.path[0]，并清空同名缓存。
单跑本阶段时这些钩子无副作用。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 需要"隔离"的同名顶层包/模块（基线 + 各阶段新增模块 + 入口模块）。
SHARED_NAMES = ("harness", "prompt", "context", "chat", "providers", "config", "server")


def _activate() -> None:
    """把本阶段目录置于导入搜索第一顺位，并清空同名顶层包/模块的缓存。"""
    here = str(HERE)
    if here in sys.path:
        sys.path.remove(here)
    sys.path.insert(0, here)
    for key in [k for k in sys.modules if k.split(".")[0] in SHARED_NAMES]:
        sys.modules.pop(key, None)


def _belongs(path) -> bool:
    """判断某个节点路径是否位于本阶段目录内。"""
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


# conftest 被直接加载（非 pytest 场景）时也激活一次。
_activate()
