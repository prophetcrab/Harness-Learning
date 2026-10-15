"""显示自述 —— 插件告诉界面"我的事件该怎么画"（壳按通用渲染器画）。

**解决的问题**：前端原先认识**具体业务词汇**。压缩插件的事件在 `app.js` 里
是写死的：

    case "compact/checkpoint": ...      // 日志事件
    case "compact_applied": ...         // 循环事件
    case "compact_failed": ...          // 失败提示
    const TERMINAL_FRAMES = new Set([..., "__compact_done", "__compact_noop"]);

于是"加一个会产出事件的插件"就得改壳——插件化的承诺在**展示层**是假的。

**做法**：插件声明 `DisplaySpec(kind, renderer, title, detail_field)`，壳里只有
**四种通用渲染器**：

    fold    可折叠的徽标（标题一行 + 展开看详情）——如"折叠 4 条消息（3152 → 717）"
    note    一行小字说明
    warn    黄色提示条（失败/警告）
    status  只更新底部状态栏，不进对话流（流式响应的收尾帧用这个）

壳不认识任何插件，只按渲染器名分派。模板用 `{字段}` 取值，壳负责格式化数字。

**为什么渲染器是固定的四种**：壳不该长成一个模板引擎。四种覆盖了"事件该怎么
显示"的全部常规形态；真需要特殊呈现的（如图表），那是插件**自己的页面**该做的事
（它已经在面板里挂了一个 iframe）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 壳内置的通用渲染器（插件只能从这几个里选——见模块说明）。
RENDERERS: tuple[str, ...] = ("fold", "note", "warn", "status")


@dataclass(frozen=True)
class DisplaySpec:
    """一种事件/帧该怎么显示。

    `kind` 既可以是**会话事件名**（进日志的，如 `compact/checkpoint`——重放历史
    时也要按它渲染），也可以是**循环事件名 / 帧名**（实时的，如 `compact_applied`、
    `__compact_done`）。两类用同一张表，前端只查一次。
    """

    #: 事件名或帧名。
    kind: str
    #: 通用渲染器名（RENDERERS 之一）。
    renderer: str = "note"
    #: 标题模板（`{字段}` 会被 payload 里的同名值替换）。
    title: str = ""
    #: `fold` 渲染器展开时显示哪个字段（如 "summary"）。
    detail_field: str = ""
    #: 排序权重（只影响台账里的顺序，便于人读）。
    order: int = 100
    #: 这个帧是不是"流的收尾"（携带结果、不渲染进对话流）。
    #: 真 = 前端只把它当返回值，不画；假 = 正常显示。
    #: 为什么需要它：读流的代码原先写死了 `__compact_done` 这种**插件专属**
    #: 的帧名来判断收尾——那是把插件的协议细节漏进了壳里。
    terminal: bool = False

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("DisplaySpec.kind 不能为空")
        if self.renderer not in RENDERERS:
            raise ValueError(
                f"未知渲染器：{self.renderer}（可用：{'、'.join(RENDERERS)}）"
                "——壳只认这几种通用渲染器，特殊呈现请放到插件自己的页面里"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "renderer": self.renderer,
            "title": self.title,
            "detail_field": self.detail_field,
            "order": self.order,
            "terminal": self.terminal,
        }


__all__ = ["RENDERERS", "DisplaySpec"]
