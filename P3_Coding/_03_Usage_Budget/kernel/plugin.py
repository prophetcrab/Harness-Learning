"""Plugin —— 插件协议与声明（name / inject / setup）。

一个插件就是"有 `setup(ctx)` 方法的对象"，或（更轻）一个普通函数/可调用对象
——沿用 P2 `_07` 的鸭子类型协议。P3 新增两样**声明**：

- `name`：插件身份（缺失时用函数名 / 类限定名兜底）；
- `inject`：依赖的服务名列表（缺失则插件保持 pending，等服务就绪自动激活；
  依赖消失则自动停用）——**装载顺序无关**，依赖关系驱动生命周期。

协议只需要"有个 setup"，插件作者不必 import 任何基类（与 LLMProvider /
FileSystem / SubprocessService 同款：结构满足即成立）。

本模块只提供"读声明、取 setup"的纯函数；装载与生命周期在 context.py
（Context.load_plugin / unload_plugin）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

# setup：装载函数，可返回 disposer（Callable[[], None]）或 None。
SetupFn = Callable[[Any], "Callable[[], None] | None"]


@runtime_checkable
class Plugin(Protocol):
    """插件协议：任何有 setup(ctx) 方法的对象都满足它。

    `name` / `inject` 是可选声明——运行时用 getattr 读取，缺失即默认值。
    """

    def setup(self, ctx: Any) -> Callable[[], None] | None:
        ...


def plugin_name(plugin: object) -> str:
    """插件名：优先 plugin.name；函数用函数名；都没有则用类限定名。"""
    name = getattr(plugin, "name", None)
    if name:
        return str(name)
    if callable(plugin) and not hasattr(plugin, "setup"):
        return str(getattr(plugin, "__name__", type(plugin).__name__))
    return f"{type(plugin).__module__}.{type(plugin).__name__}"


def plugin_setup(plugin: object) -> SetupFn:
    """取出插件的装载函数：有 setup 用 setup，否则把插件本身当函数调用。"""
    setup = getattr(plugin, "setup", None)
    if callable(setup):
        return setup
    if callable(plugin):
        return plugin
    raise TypeError(f"不是合法插件（既没有 setup(ctx) 也不能调用）：{plugin!r}")


def plugin_injects(plugin: object) -> tuple[str, ...]:
    """读取插件的依赖声明（inject）；缺失或非法一律 fail loud。

    接受两种写法：`inject = ("a", "b")`（对象属性）与函数属性 `inject`。
    """
    raw: Any = getattr(plugin, "inject", ())
    if raw is None:
        return ()
    if isinstance(raw, str):
        # 单个字符串是最常见的笔误（会被逐字符迭代）——直接报错。
        raise TypeError(
            f"插件 {plugin_name(plugin)} 的 inject 必须是序列（如 ('fs',)），收到字符串：{raw!r}"
        )
    if not isinstance(raw, Iterable):
        raise TypeError(f"插件 {plugin_name(plugin)} 的 inject 不可迭代：{raw!r}")
    names = tuple(str(item) for item in raw)
    if len(set(names)) != len(names):
        raise ValueError(f"插件 {plugin_name(plugin)} 的 inject 有重复项：{names}")
    return names


__all__ = ["Plugin", "SetupFn", "plugin_injects", "plugin_name", "plugin_setup"]
