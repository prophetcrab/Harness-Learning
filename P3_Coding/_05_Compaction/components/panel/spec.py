"""面板自述 —— 插件告诉界面"我该长什么样、挂在哪"（`_05` 收尾补齐）。

**解决的问题（耦合缺口）**：后端早就做到了"界面不认识任何插件"——服务自己暴露
地址，界面读 `endpoints` 发现。但**前端还是写死的**：

    { service: "context-meter", ... }   // 📐 面板
    { service: "token-usage",   ... }   // 📊 面板

加第三个面板插件就得改壳。这份构件把"挂载点"从**壳里**挪回**插件自己**：

    插件 → 声明 PanelSpec（按钮文字、读哪个 JSON、徽标怎么算）
    内核 → 台账里带给页面
    壳   → 遍历，谁声明就挂谁（**零知识**）

**为什么要一个独立构件而不是各写各的**：面板的"形状"是壳与插件之间的契约，
两边必须对同一套字段达成一致；散在各插件里各写一个 dict，字段名一漂移
（`path` vs `url`）就静默不显示。放一处，两边 import 同一份定义。

**为什么用 dataclass 而不是 Protocol**：这里描述的是**数据**（一串可序列化的
字段），不是行为——内核要把它塞进 JSON 交给浏览器，所以它必须能 `to_dict()`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: 徽标数字的取值方式（壳按这个格式解析插件给的 JSON）。
#:
#: 刻意只有两种最简单的方式——壳不该懂业务：
#:   "percent"  取 JSON 里某个字段当百分比（如 used/capacity 已算好的 percent）
#:   "number"   取 JSON 里某个字段当计数（如调用次数、总 token）
#: 更复杂的展示（多段进度条、明细表）留给**插件自己的页面**——那才是
#: 它该负责呈现的地方，壳只提供一个入口按钮。
BadgeKind = Literal["percent", "number"]


@dataclass(frozen=True)
class PanelSpec:
    """一个面板的"挂载点自述"（插件声明，界面消费）。

    字段刻意少：壳要做的只有"放个按钮、点开显示插件的页面、按钮上带个数"。
    """

    #: 服务的名字（= `endpoints` 里的 key；壳靠它找回地址）。
    service: str
    #: 按钮文字（带图标，如 "📐 上下文"）。
    label: str
    #: 面板标题（点开后的框头）。
    title: str
    #: 徽标：读插件页面的哪个 JSON 接口、取哪个字段、怎么显示。
    badge_path: str = ""          # 相对服务地址的路径，如 "api/context"
    badge_field: str = ""         # 从该 JSON 顶层取的字段名
    badge_kind: BadgeKind = "number"
    badge_suffix: str = ""        # 显示后缀（如 "%"）
    #: 徽标读不到时的降级文字（服务在但没数据，或服务没起来）。
    badge_fallback: str = ""
    #: 排序权重（小的排前面；同值按 service 名字典序，保证顺序稳定）。
    order: int = 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "label": self.label,
            "title": self.title,
            "badge_path": self.badge_path,
            "badge_field": self.badge_field,
            "badge_kind": self.badge_kind,
            "badge_suffix": self.badge_suffix,
            "badge_fallback": self.badge_fallback or self.label,
            "order": self.order,
        }


def panels_of(service: Any) -> list[PanelSpec]:
    """从一个已提供的服务对象上读出它自述的面板（没有则空列表）。

    服务的可选属性：`panels`——一个 `PanelSpec` 序列（或单个）。内核不认识任何
    具体插件，只按这个方法约定的名字去闻；插件加面板只需在自己那个端点对象上
    挂 `panels = (...)`,**不用改壳也不用改内核**。

    **按结构判断，不用 isinstance**：`PanelSpec` 这个类对象在不同"模块副本"里
    可能不是同一个（本项目的阶段目录各自持有同名包，测试的 conftest 会驱逐
    `sys.modules` 缓存；`_02` 的 `ScriptedStream` 踩过完全一样的坑）。
    `isinstance` 在这种情形下会**静默返回 False**——面板就这么无声无息地没了，
    正是本模块要消灭的那类故障。判据改成"长得像不像"：有 `to_dict()` 且能被
    序列化出 `service` 字段。
    """
    raw = getattr(service, "panels", None)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if _looks_like_panel(item)]
    return [raw] if _looks_like_panel(raw) else []


def _looks_like_panel(value: Any) -> bool:
    """鸭子类型的 PanelSpec 判据（跨模块副本也成立）。"""
    if not callable(getattr(value, "to_dict", None)):
        return False
    try:
        data = value.to_dict()
    except Exception:  # noqa: BLE001 —— 自述写坏了不该炸整个台账
        return False
    return isinstance(data, dict) and isinstance(data.get("service"), str) and bool(data["service"])


__all__ = ["BadgeKind", "PanelSpec", "panels_of"]
