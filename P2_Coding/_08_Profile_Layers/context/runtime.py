"""RuntimeContext —— 一次渲染时刻的"运行时环境"快照。

本阶段新增的核心词汇。它回答的问题是："**这一次**渲染，`{{cwd}}` / `{{platform}}` /
`{{time}}` 分别是什么？"

为什么做成"快照"而不是"采集器"：渲染必须是纯函数（同样的快照 → 同样的文本），
而"什么时候重新采集"是策略——归接线层（每 step 采集一次，见 `provider.py`）。
于是测试可以喂脚本化的时钟得到确定性快照，真实运行用 `collect_runtime_context()`。

对应 dsh：`packages/context/time-context`（每 step 一条带时间戳的上下文读数）。
"""

from __future__ import annotations

import platform as platform_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RuntimeContext:
    """渲染时刻的运行时环境：三个变量，全部已格式化为字符串。"""

    cwd: str
    platform: str
    time: str

    def variables(self) -> dict[str, str]:
        """交给插值器的变量表（键名 = section 里 {{...}} 的名字）。"""
        return {"cwd": self.cwd, "platform": self.platform, "time": self.time}


def collect_runtime_context(
    workspace: str | Path,
    *,
    now: Callable[[], datetime] | None = None,
    platform_name: str | None = None,
) -> RuntimeContext:
    """采集"当下"这一刻的运行时环境。

    workspace 是文件工具的工作区根目录（{{cwd}} 取它的绝对路径）——模型用相对
    路径操作文件时需要一个明确的"根"。now / platform_name 可注入（测试与演示
    用脚本化取值得到确定性输出；真实运行走默认值）。
    """
    moment = (now or datetime.now)().astimezone()
    return RuntimeContext(
        cwd=str(Path(workspace).resolve()),
        platform=platform_name or platform_module.system(),
        time=moment.isoformat(timespec="seconds"),
    )


__all__ = ["RuntimeContext", "collect_runtime_context"]
