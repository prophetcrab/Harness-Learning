"""发现 —— 从服务对象上读出插件的自述（面板 / 显示 / 路由）。

**一条统一的规则**：插件把自己想告诉界面的东西，挂在自己**提供的那个服务对象**
上（或挂在一个专门的 `Surface` 上）：

    ctx.provide("token-usage", UsageEndpoint(..., panels=(PanelSpec(...),)))
    ctx.provide("compact", Surface(display=(...), routes=(...)))

壳（webui）遍历 `ctx.services`，对每个服务问三个问题：有 `panels` 吗？有
`display` 吗？有 `routes` 吗？——**壳不认识任何具体插件**。

**为什么判据是鸭子类型而不是 isinstance**：类对象在不同"模块副本"里可能不是
同一个（本项目阶段目录各自持有同名包，测试的 conftest 会驱逐 `sys.modules`）。
`isinstance` 在这种情况下会**静默返回 False**，于是自述无声消失——这正是本层
要消灭的那类故障（`_02` 的 ScriptedStream、`_05` 的 PanelSpec 各踩过一次）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from components.shell.display import DisplaySpec
from components.shell.panel import PanelSpec
from components.shell.routes import RouteSpec


@dataclass(frozen=True)
class Surface:
    """插件的"界面自述"载体（当服务对象本身不方便挂这些属性时用）。

    典型场景：插件提供的是一个 `components` 层的对象（如 `Compactor`），
    那是别人的类、不该为界面契约改它。这时另外 provide 一个 Surface 就够：

        ctx.provide("compact.surface", Surface(display=(...), routes=(...)))

    三个字段都可以留空——只声明用到的那部分。
    """

    panels: tuple[PanelSpec, ...] = ()
    display: tuple[DisplaySpec, ...] = ()
    routes: tuple[RouteSpec, ...] = ()


def _sequence_of(raw: Any, looks_like) -> list[Any]:
    """把"单个 / 序列 / 空"统一成列表，并逐项按 `looks_like` 过滤。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [item for item in raw if looks_like(item)]
    return [raw] if looks_like(raw) else []


def _looks_like_panel(value: Any) -> bool:
    if not callable(getattr(value, "to_dict", None)):
        return False
    try:
        data = value.to_dict()
    except Exception:  # noqa: BLE001 —— 自述写坏了不该炸掉整个台账
        return False
    return isinstance(data, dict) and isinstance(data.get("service"), str) and bool(data["service"])


def _looks_like_display(value: Any) -> bool:
    if not callable(getattr(value, "to_dict", None)):
        return False
    try:
        data = value.to_dict()
    except Exception:  # noqa: BLE001
        return False
    return isinstance(data, dict) and isinstance(data.get("kind"), str) and bool(data["kind"])


def _looks_like_route(value: Any) -> bool:
    # 路由的判据不能靠 to_dict（handler 不可序列化，但 spec 上一定有 method/pattern）
    return (
        isinstance(getattr(value, "method", None), str)
        and isinstance(getattr(value, "pattern", None), str)
        and callable(getattr(value, "handler", None))
    )


def panels_of(service: Any) -> list[PanelSpec]:
    """服务自述的面板（没有则空列表）。"""
    return _sequence_of(getattr(service, "panels", None), _looks_like_panel)


def display_of(service: Any) -> list[DisplaySpec]:
    """服务自述的显示规则（没有则空列表）。"""
    return _sequence_of(getattr(service, "display", None), _looks_like_display)


def routes_of(service: Any) -> list[RouteSpec]:
    """服务自述的路由（没有则空列表）。"""
    return _sequence_of(getattr(service, "routes", None), _looks_like_route)


__all__ = ["Surface", "display_of", "panels_of", "routes_of"]
