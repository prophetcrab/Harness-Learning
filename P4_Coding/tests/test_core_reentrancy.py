"""重入与混用：setup 里装卸插件、事件里改世界、注入清理异常。"""

from __future__ import annotations

import pytest
from support import assert_steady, info, load, make_plugin

from core import Context


async def test_setup_can_load_another_plugin(ctx: Context) -> None:
    class Host:
        name = "host"

        async def setup(self, c: Context) -> None:
            await c.load_plugin(make_plugin("guest", provides=("g-out",)))

    await ctx.load_plugin(Host())
    assert set(ctx.active_plugins) == {"host", "guest"}
    assert ctx.require("g-out") == "guest"
    assert_steady(ctx)


async def test_setup_can_unload_existing_plugin(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("old", provides=("s",)))

    class Replacer:
        name = "replacer"

        async def setup(self, c: Context) -> None:
            await c.unload_plugin("old")

    await ctx.load_plugin(Replacer())
    assert "old" not in [item.name for item in ctx.plugins]
    assert "s" not in ctx.services
    assert_steady(ctx)


async def test_event_handler_can_change_world(ctx: Context) -> None:
    waiting = make_plugin("waiting", inject=("s",))
    await load(ctx, waiting)
    assert info(ctx, "waiting").status == "pending"

    async def on_ready(**kw: object) -> None:
        await ctx.provide("s", 1)

    ctx.on("ready", on_ready)
    await ctx.emit("ready")

    assert info(ctx, "waiting").status == "active"
    assert_steady(ctx)


async def test_injection_disposer_error_does_not_block_others(ctx: Context) -> None:
    stopped: list[str] = []

    def bad_callback(c: Context) -> object:
        return lambda: (_ for _ in ()).throw(RuntimeError("stop boom"))

    await ctx.inject(["dep"], bad_callback)
    await ctx.inject(["dep"], lambda c: stopped.append("good"))

    await ctx.provide("dep", 1)
    assert all(item.status == "active" for item in ctx.injections)

    with pytest.raises(RuntimeError, match="stop boom"):
        await ctx.retract("dep")

    assert [item.status for item in ctx.injections] == ["pending", "pending"]
    assert stopped == ["good"]
    assert_steady(ctx)


async def test_reload_provider_reactivates_pending_dependents(ctx: Context) -> None:
    await load(ctx, make_plugin("provider", provides=("s",)))
    await load(ctx, make_plugin("user", inject=("s",)))
    await ctx.unload_plugin("provider")

    assert info(ctx, "user").status == "pending"
    assert_steady(ctx)

    await ctx.load_plugin(make_plugin("provider", provides=("s",)))
    assert info(ctx, "user").status == "active"
    assert_steady(ctx)


async def test_disable_returns_cascade_names(ctx: Context) -> None:
    await load(ctx, make_plugin("provider", provides=("a",)))
    await load(ctx, make_plugin("middle", inject=("a",), provides=("b",)))
    await load(ctx, make_plugin("leaf", inject=("b",)))

    affected = await ctx.disable_plugin("provider")
    assert affected[0] == "provider"
    assert set(affected) == {"provider", "middle", "leaf"}
    assert_steady(ctx)


async def test_effect_can_be_create_and_unload_inside_plugin_setup(ctx: Context) -> None:
    cleaned: list[str] = []

    class P:
        name = "p"

        async def setup(self, c: Context) -> None:
            handle = await c.effect("temp", lambda: (lambda: cleaned.append("temp")))
            await handle()

    await ctx.load_plugin(P())
    assert cleaned == ["temp"]
    assert ctx.effects == ["plugin:p"]
    assert_steady(ctx)
