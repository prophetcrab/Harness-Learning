"""plugin —— 插件协议与声明读取。

插件是满足以下形状的对象（鸭子类型，不必继承）：

    class MyPlugin:
        name = "my-plugin"            # 台账键与报错称呼；缺省用类名
        inject = ("store",)           # 必需服务：缺了插件保持 pending
        optional = ("approval",)      # 可选服务：缺了照常激活，来了不重装
        requires = ("llm-provider",)  # 插件级依赖：只做装配期校验
        async def setup(self, ctx):     # 也可以是同步函数
            await ctx.provide("greeting", "你好")

setup 在装载期执行（core 会 await 异步的 setup），可以返回撤销函数或 None。

推荐用 `async def setup`：provide / effect / inject / load_plugin 等生命周期 API
都是 awaitable，同步 setup 里调用它们会得到未 await 的协程（只会产生警告）。
同步 setup 只适合纯计算、或只做同步登记（如 ctx.on）。

各声明均可省略。本模块提供读取与校验这些声明的纯函数，非法声明在读取时报错；
插件的生命周期在 context.py。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

#: 撤销函数：同步或异步（返回 awaitable 时 core 会等待）；也可以返回 None。
Disposer = Callable[[], Any]
#: 装载函数：同步或异步；返回撤销函数、None 或它们的 awaitable。
SetupFn = Callable[[Any], Any]


class Plugin(Protocol):
    """插件协议：有 setup(ctx) 就是插件（同步/异步均可）；其余声明可选。"""

    def setup(self, ctx: Any) -> Any:
        ...


def plugin_name(plugin: object) -> str:
    """插件名：优先 plugin.name，其次函数名，最后类限定名。

    台账按名字记账：一个运行时里一个插件名只保留一份实例。
    """
    name = getattr(plugin, "name", None)
    if name:
        return str(name)
    if callable(plugin) and not hasattr(plugin, "setup"):
        return str(getattr(plugin, "__name__", type(plugin).__name__))
    cls = type(plugin)
    return f"{cls.__module__}.{cls.__qualname__}"


def plugin_setup(plugin: object) -> SetupFn:
    """取出装载函数：优先 setup；否则把插件本身当函数；都不可调用则 TypeError。"""
    setup = getattr(plugin, "setup", None)
    if callable(setup):
        return setup
    if callable(plugin):
        return plugin
    raise TypeError(f"不是插件（既没有 setup(ctx) 也不能调用）：{plugin!r}")


def _service_names(plugin: object, attr: str) -> tuple[str, ...]:
    """读取一组服务名声明；字符串、不可迭代、空名、重复都报错。"""
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

    与 inject 重叠时报错（一个服务不能既必需又可选）。
    """
    optionals = _service_names(plugin, "optional")
    overlap = sorted(set(optionals) & set(plugin_injects(plugin)))
    if overlap:
        raise ValueError(
            f"插件 {plugin_name(plugin)} 的 optional 与 inject 重叠：{overlap}"
            "（一个服务要么必需、要么可选）"
        )
    return optionals


#: 提供者插件的命名约定：`llm-provider` 的实例可以叫 `llm-provider:deepseek`。
PROVIDER_SEPARATOR = ":"


def matches_plugin_requirement(required: str, plugin_name_: str) -> bool:
    """插件名是否满足一条插件级依赖：同名，或以 "required:" 开头。"""
    return plugin_name_ == required or plugin_name_.startswith(
        f"{required}{PROVIDER_SEPARATOR}"
    )


def plugin_requires(plugin: object) -> tuple[str, ...]:
    """插件级依赖（requires）：只做装配期校验，不参与激活。

    与 inject / optional 的区别：那两者声明服务并驱动生命周期；requires 声明
    插件，缺了不会 pending，由装配方检查。
    """
    return _service_names(plugin, "requires")
