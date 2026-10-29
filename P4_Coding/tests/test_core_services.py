"""服务槽：提供 / 查找 / 撤销，以及撤销引发的退位顺序与回卷归属。"""

from __future__ import annotations

import pytest
from support import assert_steady, info, load, make_plugin

from core import Context, DuplicateSlotError, MissingServiceError


async def test_provide_get_require_and_retract(ctx: Context) -> None:
    await ctx.provide("a", 1)
    assert ctx.get("a") == 1
    assert ctx.require("a") == 1
    assert ctx.services == ["a"]

    assert await ctx.retract("a") == 1
    assert ctx.services == []
    assert ctx.get("a") is None
    assert ctx.get("a", 0) == 0

    with pytest.raises(MissingServiceError):
        ctx.require("a")
    with pytest.raises(MissingServiceError):
        await ctx.retract("a")


async def test_provide_duplicate_keeps_original_value(ctx: Context) -> None:
    await ctx.provide("a", 1)
    with pytest.raises(DuplicateSlotError):
        await ctx.provide("a", 2)
    assert ctx.require("a") == 1


async def test_none_valued_service(ctx: Context) -> None:
    await ctx.provide("a", None)
    assert ctx.require("a") is None
    assert ctx.get("a", "default") is None
    await ctx.retract("a")
    assert "a" not in ctx.services


async def test_service_key_validation(ctx: Context) -> None:
    with pytest.raises(ValueError):
        await ctx.provide("", 1)
    with pytest.raises(ValueError):
        await ctx.provide(1, 1)  # type: ignore[arg-type]


async def test_services_snapshot_in_provide_order(ctx: Context) -> None:
    await ctx.provide("b", 1)
    await ctx.provide("a", 2)
    snapshot = ctx.services
    assert snapshot == ["b", "a"]
    snapshot.append("c")  # 改快照不影响容器
    assert ctx.services == ["b", "a"]


async def test_retract_retires_dependents_deep_first(ctx: Context) -> None:
    """bug 修复：retract 级联也要"深的先退"，依赖者清理时它的服务还在。"""
    seen: dict[str, set[str]] = {}

    class Provider:
        name = "provider"
        inject = ("root",)

        async def setup(self, c: Context) -> object:
            await c.provide("a-out", 1)
            return lambda: seen.update({"provider": set(c.services)})

    class Consumer:
        name = "consumer"
        inject = ("a-out",)

        def setup(self, c: Context) -> object:
            return lambda: seen.update({"consumer": set(c.services)})

    await ctx.provide("root", 1)
    await ctx.load_plugin(Provider())
    await ctx.load_plugin(Consumer())

    await ctx.retract("root")

    assert list(seen) == ["consumer", "provider"]
    assert "a-out" in seen["consumer"]
    assert_steady(ctx)


async def test_retract_moves_dependents_to_pending_and_recovers(ctx: Context) -> None:
    provider = make_plugin("provider", provides=("s",))
    user = make_plugin("user", inject=("s",))
    await load(ctx, provider, user)
    assert ctx.active_plugins == ["provider", "user"]

    await ctx.retract("s")
    assert info(ctx, "user").status == "pending"
    assert info(ctx, "user").missing == ("s",)
    assert_steady(ctx)

    await ctx.provide("s", "new")
    assert info(ctx, "user").status == "active"
    assert_steady(ctx)


async def test_slot_rollback_only_removes_own_provision(ctx: Context) -> None:
    """bug 修复：retract 后由别人重新提供的槽，不被旧 effect 的回卷误删。"""
    first = make_plugin("first", provides=("k",), values={"k": "first-value"})
    await ctx.load_plugin(first)
    await ctx.retract("k")

    second = make_plugin("second", provides=("k",), values={"k": "second-value"})
    await ctx.load_plugin(second)
    assert ctx.require("k") == "second-value"

    await ctx.unload_plugin("first")
    assert ctx.require("k") == "second-value"
    assert_steady(ctx)


async def test_retract_updates_owner_provided_and_avoids_false_cascade(ctx: Context) -> None:
    """bug 修复：retract 后旧提供者的 provided 要清掉，卸载它时不能误伤新依赖者。"""
    log: list[str] = []
    old = make_plugin("old", provides=("k",))
    new = make_plugin("new", provides=("k",))
    user = make_plugin("user", inject=("k",), log=log)

    await load(ctx, old)
    await ctx.retract("k")
    await load(ctx, new, user)
    assert info(ctx, "old").provided == ()
    assert log == ["user:setup"]

    await ctx.unload_plugin("old")
    assert "user" in ctx.active_plugins
    assert log == ["user:setup"]  # 没有多余的退位/重装
    assert_steady(ctx)


async def test_provide_outside_effect_is_not_auto_revoked(ctx: Context) -> None:
    await ctx.provide("a", 1)
    await ctx.effect("e", lambda: None)
    await ctx.unload_effect("e")
    assert ctx.require("a") == 1


async def test_provide_inside_effect_is_auto_revoked(ctx: Context) -> None:
    async def install() -> None:
        await ctx.provide("a", 1)

    await ctx.effect("e", install)
    assert ctx.require("a") == 1
    await ctx.unload_effect("e")
    assert "a" not in ctx.services
