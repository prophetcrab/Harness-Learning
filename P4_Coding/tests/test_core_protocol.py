"""插件协议：鸭子类型、声明校验、函数插件、命名规则。"""

from __future__ import annotations

import pytest
from support import assert_steady

from core import (
    Context,
    DuplicatePluginError,
    MissingPluginError,
    matches_plugin_requirement,
    plugin_injects,
    plugin_name,
    plugin_optionals,
    plugin_requires,
    plugin_setup,
)


async def test_function_is_a_plugin(ctx: Context) -> None:
    cleaned: list[str] = []

    async def fn_plugin(c: Context) -> object:
        await c.provide("fn-out", 1)
        return lambda: cleaned.append("fn")

    await ctx.load_plugin(fn_plugin)
    assert ctx.require("fn-out") == 1
    assert ctx.active_plugins == ["fn_plugin"]

    await ctx.unload_plugin("fn_plugin")
    assert cleaned == ["fn"]
    assert_steady(ctx)


def test_plugin_name_fallbacks() -> None:
    class Named:
        name = "explicit"

    class Anon:
        pass

    def bare() -> None:
        pass

    assert plugin_name(Named()) == "explicit"
    assert plugin_name(bare) == "bare"
    assert plugin_name(Anon()).endswith(".Anon")


async def test_plugin_setup_rejects_non_plugin() -> None:
    with pytest.raises(TypeError):
        plugin_setup(object())
    with pytest.raises(TypeError):
        await Context().load_plugin(object())


def test_declaration_validation() -> None:
    class StrInject:
        inject = "fs"  # type: ignore[assignment]

    class EmptyService:
        inject = ("",)  # type: ignore[assignment]

    class DuplicateService:
        inject = ("a", "a")

    class StrRequires:
        requires = "llm-provider"  # type: ignore[assignment]

    class BadOptional:
        inject = ("a",)
        optional = ("b", "b")

    class Overlap:
        inject = ("a",)
        optional = ("a",)

    with pytest.raises(TypeError):
        plugin_injects(StrInject())
    with pytest.raises(ValueError):
        plugin_injects(EmptyService())
    with pytest.raises(ValueError):
        plugin_injects(DuplicateService())
    with pytest.raises(ValueError):
        plugin_optionals(BadOptional())
    with pytest.raises(ValueError):
        plugin_optionals(Overlap())
    with pytest.raises(TypeError):
        plugin_requires(StrRequires())


def test_default_declarations_are_empty() -> None:
    class Bare:
        def setup(self, ctx: Context) -> None:
            pass

    plugin = Bare()
    assert plugin_injects(plugin) == ()
    assert plugin_optionals(plugin) == ()
    assert plugin_requires(plugin) == ()


def test_plugin_requirement_matching() -> None:
    assert matches_plugin_requirement("llm-provider", "llm-provider")
    assert matches_plugin_requirement("llm-provider", "llm-provider:deepseek")
    assert not matches_plugin_requirement("llm-provider", "llm-provider-extra")
    assert not matches_plugin_requirement("llm-provider", "other")


async def test_one_instance_per_name(ctx: Context) -> None:
    class Same:
        def setup(self, ctx: Context) -> None:
            pass

    first = Same()
    second = Same()
    await ctx.load_plugin(first)
    with pytest.raises(DuplicatePluginError):
        await ctx.load_plugin(second)


async def test_explicit_name_stable(ctx: Context) -> None:
    class P:
        name = "stable"

        def setup(self, ctx: Context) -> None:
            pass

    await ctx.load_plugin(P())
    with pytest.raises(DuplicatePluginError):
        await ctx.load_plugin(P())
    await ctx.unload_plugin("stable")
    with pytest.raises(MissingPluginError):
        await ctx.unload_plugin("stable")


async def test_inject_subscription_is_reregistered_by_enable(ctx: Context) -> None:
    log: list[str] = []

    class P:
        name = "p"

        async def setup(self, c: Context) -> None:
            log.append("setup")
            await c.inject(
                ["dep"],
                lambda cc: (log.append("inject-start"), lambda: log.append("inject-stop"))[1],
            )

    await ctx.load_plugin(P())
    await ctx.provide("dep", 1)
    assert log == ["setup", "inject-start"]
    assert [item.status for item in ctx.injections] == ["active"]

    await ctx.disable_plugin("p")
    assert log == ["setup", "inject-start", "inject-stop"]
    assert ctx.injections == []  # 订阅随插件 effect 回卷被取消

    await ctx.enable_plugin("p")
    assert log == ["setup", "inject-start", "inject-stop", "setup", "inject-start"]
    assert [item.status for item in ctx.injections] == ["active"]
    assert_steady(ctx)


def test_none_declarations_mean_empty() -> None:
    class P:
        inject = None  # type: ignore[assignment]
        optional = None  # type: ignore[assignment]
        requires = None  # type: ignore[assignment]

        def setup(self, ctx: Context) -> None:
            pass

    plugin = P()
    assert plugin_injects(plugin) == ()
    assert plugin_optionals(plugin) == ()
    assert plugin_requires(plugin) == ()


def test_non_iterable_declaration_is_rejected() -> None:
    class P:
        inject = 42  # type: ignore[assignment]

        def setup(self, ctx: Context) -> None:
            pass

    with pytest.raises(TypeError):
        plugin_injects(P())
