"""plugin —— 插件协议与声明（name / inject / optional / setup）。

**插件是什么**：一个有 `setup(ctx)` 方法的对象——不必 import 任何基类，结构满足
即成立（鸭子类型）。setup 在装载期执行，把插件的一切注册进 ctx；它可以返回一个
撤销函数（`Disposer`），卸载时会被调用。

**三样声明**（都可选，缺了用默认值）：

    name      身份：台账的键、报错里的称呼。缺省用类名兜底。
    inject    必需依赖（服务名序列）：缺了 → pending（依赖齐了自动激活、提供者走了自动退位）
    optional  可选依赖：缺了**照常激活**、来了**不重装**——"有则用、没有就降级"，
              取不到时用 ctx.get 容错。为什么"来了不重装"：可选服务抖一下不该重启
              一个正在对外服务的插件（P3 的界面插件就是这个场景——它要在换模型、
              装卸插件的动荡里一直开着，而不是让用户的页面失联）。

本模块只提供"读声明"的纯函数：不认识 harness（无业务词汇），也不认识 loader
（条目 / 角色是 loader 的数据）。生命周期在 context.py。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

#: setup 的返回：撤销函数，或 None（"东西都注册在 ctx 上，没有额外资源要收"）。
Disposer = Callable[[], None]
#: setup 的签名：收到 ctx，返回撤销函数或 None。
SetupFn = Callable[[Any], Disposer | None]


class Plugin(Protocol):
    """插件协议：有 setup(ctx) 就是插件。

    `name` / `inject` / `optional` 是**可选声明**——运行时按鸭子类型读取，
    缺失即默认值（见本模块各读取函数）。
    """

    def setup(self, ctx: Any) -> Disposer | None:
        ...


def plugin_name(plugin: object) -> str:
    """插件名：优先 `plugin.name`；函数用函数名；都没有则用类限定名。

    台账按名字记账（一个运行时里一个插件名一份实例），所以名字必须稳定、可读、
    可重装——而不是内存地址这类东西。
    """
    name = getattr(plugin, "name", None)
    if name:
        return str(name)
    if callable(plugin) and not hasattr(plugin, "setup"):
        return str(getattr(plugin, "__name__", type(plugin).__name__))
    cls = type(plugin)
    return f"{cls.__module__}.{cls.__qualname__}"


def plugin_setup(plugin: object) -> SetupFn:
    """取出装载函数：有 `setup` 用它；否则把插件本身当函数用；再不然 fail loud。"""
    setup = getattr(plugin, "setup", None)
    if callable(setup):
        return setup
    if callable(plugin):
        return plugin
    raise TypeError(f"不是插件（既没有 setup(ctx) 也不能调用）：{plugin!r}")


def _service_names(plugin: object, attr: str) -> tuple[str, ...]:
    """读一组"服务名序列"声明（inject / optional 共用）：非法一律 fail loud。"""
    raw: Any = getattr(plugin, attr, ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        # 单个字符串是最常见笔误（会被逐字符迭代）——直接报错，别让它静默生效。
        raise TypeError(
            f"插件 {plugin_name(plugin)} 的 {attr} 必须是序列（如 ('fs',)），"
            f"收到字符串：{raw!r}"
        )
    if not isinstance(raw, Iterable):
        raise TypeError(f"插件 {plugin_name(plugin)} 的 {attr} 不可迭代：{raw!r}")
    names = tuple(str(item) for item in raw)
    if any(not name for name in names):
        raise ValueError(f"插件 {plugin_name(plugin)} 的 {attr} 里有空的服务名：{names!r}")
    if len(set(names)) != len(names):
        raise ValueError(f"插件 {plugin_name(plugin)} 的 {attr} 有重复项：{names!r}")
    return names


def plugin_injects(plugin: object) -> tuple[str, ...]:
    """必需依赖（inject）：缺了插件保持 pending。"""
    return _service_names(plugin, "inject")


def plugin_optionals(plugin: object) -> tuple[str, ...]:
    """可选依赖（optional）：缺了照常激活、来了不重装。

    与 inject 重叠直接报错：一个服务不可能既必需又可选——声明矛盾说明作者还没
    想清楚（装载期报错比运行时的古怪行为便宜）。
    """
    optionals = _service_names(plugin, "optional")
    overlap = sorted(set(optionals) & set(plugin_injects(plugin)))
    if overlap:
        raise ValueError(
            f"插件 {plugin_name(plugin)} 的 optional 与 inject 重叠：{overlap}"
            "（一个服务要么必需、要么可选）"
        )
    return optionals
