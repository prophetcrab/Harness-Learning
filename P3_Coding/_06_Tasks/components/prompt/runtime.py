"""RuntimeContext —— 一次渲染时刻的"运行时环境"快照。

它回答的问题是："**这一次**渲染，`{{cwd}}` / `{{platform}}` / `{{date}}` /
`{{time}}` 分别是什么？"

**`date` 与 `time` 的取舍（`_04` 修复的核心）**：系统提示词是请求的**第一条**
消息，前缀缓存从它开始比对。默认提示词引用的变量只要每步都变，缓存就**永远**
不命中（实测：稳定前缀 564 tokens 第二次起命中 384；把这一刻的秒级时间写进去，
命中 0）。所以：

- `{{date}}`（YYYY-MM-DD）：**一天之内不变**——默认提示词用它，让前缀稳定；
- `{{time}}`（精确到秒）：仍然提供（`_02` 的能力没删），但它属于"每步都变"的
  内容，**默认不引用**；确实需要精确时间的场景应自己承担缓存成本，
  或改用工具（`now`）在需要时取。

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
    """渲染时刻的运行时环境：四个变量，全部已格式化为字符串。"""

    cwd: str
    platform: str
    time: str
    #: 日期（YYYY-MM-DD）。默认提示词引用它——**一天内不变，缓存友好**。
    #: 留空时从 time 推导（兼容只给 time 的旧调用点与测试）。
    date: str = ""

    def __post_init__(self) -> None:
        if not self.date and self.time:
            object.__setattr__(self, "date", self.time[:10])

    def variables(self) -> dict[str, str]:
        """交给插值器的变量表（键名 = section 里 {{...}} 的名字）。"""
        return {
            "cwd": self.cwd,
            "platform": self.platform,
            "date": self.date,
            "time": self.time,
        }


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
        date=moment.date().isoformat(),
    )


__all__ = ["RuntimeContext", "collect_runtime_context"]
