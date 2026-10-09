"""Plugin —— 插件协议与装载器：`setup(ctx) -> disposer`，装载即注册 effect。

M6 的第二个词汇（与 Context 配套）。一个插件就是实现了 `setup(ctx)` 的对象
（或在 Python 里更轻：一个普通函数/可调用对象，鸭子类型）。协议极简：

    class MyPlugin:
        name = "my-plugin"
        def setup(self, ctx: Context) -> Disposer | None:
            ctx.provide("greeting", "你好")
            return lambda: ctx.retract("greeting")   # 可选：显式撤销动作

    ...

    ctx = Context()
    dispose = load_plugin(ctx, MyPlugin())   # ← 装载：包成一个 effect
    dispose()                                # ← 卸载：自动回卷 setup 装过的一切

两个装载策略（loader 的全部内容）：

- `load_plugin(ctx, plugin)`：给一个插件对象/函数，包成 effect 并装载。
  插件的名字取自 `plugin.name`（缺失则用模块限定名），effect 名形如 "plugin:xxx"。
- `load_plugins(ctx, plugins)`：批量装载；**任一失败则回卷已装成功的部分**
  （半装状态比报错更糟——这是 effect 栈的价值所在）。

`setup` 可以三种写法（`Plugin` 协议只需要"有 setup 或本身可调用"）：
1. 类实例：`setup(ctx)` 方法；
2. 普通函数：`def setup(ctx): ...`（函数名作为插件名）；
3. 返回 disposer 或 None —— 后者表示"我的注册都通过 ctx.effect 管理"。

对应 dsh：Cordis 的 "A plugin is an object that implements Service" /
"Registrations are reversible effects"；本实现刻意朴素——只有 ctx + effect，
没有 Cordis 的 inject 依赖声明、事件瀑布、Service 生命周期（那些留给后续）。

依赖方向：`kernel → 无`（纯逻辑包，不依赖 harness / prompt / context / providers）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from kernel.context import Context, Disposer

# 一个插件：有 setup(ctx) 方法的对象，或者（见下）本身就是一个函数。
SetupFn = Callable[[Context], "Disposer | None"]


@runtime_checkable
class Plugin(Protocol):
    """插件协议：任何有 setup(ctx) 方法的对象都满足它。

    用 Protocol 而非基类（与项目中 LLMProvider / FileSystem 同款）：插件作者
    不必 import 任何东西就能写插件——只要有个 setup 方法。
    """

    name: str

    def setup(self, ctx: Context) -> Disposer | None:
        ...


def _plugin_name(plugin: object) -> str:
    """插件名：优先 plugin.name；函数用函数名；都没有则用类限定名。"""
    if hasattr(plugin, "name"):
        return str(plugin.name)
    if isinstance(plugin, Callable) and not hasattr(plugin, "setup"):
        return getattr(plugin, "__name__", type(plugin).__name__)
    return f"{type(plugin).__module__}.{type(plugin).__name__}"


def _plugin_setup(plugin: object) -> SetupFn:
    """取出插件的装载函数：有 setup 用 setup，否则把插件本身当函数调用。"""
    setup = getattr(plugin, "setup", None)
    if callable(setup):
        return setup
    if callable(plugin):
        return plugin
    raise TypeError(
        f"不是合法插件（既没有 setup(ctx) 也不能调用）：{plugin!r}"
    )


def load_plugin(ctx: Context, plugin: object) -> Disposer:
    """装载一个插件（包成 effect）；返回卸载函数（调用即自动回卷）。

    插件本身是重复装载的（同名 effect 会报错），"能否装第二次"由插件决定——
    装载器只保证"卸载可预测"。
    """
    name = f"plugin:{_plugin_name(plugin)}"
    setup = _plugin_setup(plugin)
    return ctx.effect(name, lambda: setup(ctx))


def load_plugins(ctx: Context, plugins: Sequence[object]) -> list[Disposer]:
    """按顺序批量装载；任一失败 → 回卷已装成功的部分，再把异常抛出。"""
    loaded: list[Disposer] = []
    try:
        for plugin in plugins:
            loaded.append(load_plugin(ctx, plugin))
    except BaseException:
        for dispose in reversed(loaded):  # 逆序回卷（后装的先撤）
            dispose()
        raise
    return loaded


__all__ = ["Plugin", "SetupFn", "load_plugin", "load_plugins"]
