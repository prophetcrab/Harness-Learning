"""动态依赖 ctx.inject：依赖齐了才跑、走了拆、失败标记与重试。"""

from __future__ import annotations

import pytest
from support import assert_steady, info, load, make_plugin

from core import Context


async def test_inject_runs_when_dependency_appears_and_stops_when_gone(
    ctx: Context,
) -> None:
    log: list[str] = []

    async def callback(c: Context) -> object:
        log.append("start")
        await c.provide("late", 1)
        return lambda: log.append("stop")

    await ctx.inject(["store"], callback)
    assert [item.status for item in ctx.injections] == ["pending"]
    assert ctx.injections[0].missing == ("store",)

    await ctx.provide("store", 1)
    assert log == ["start"]
    assert [item.status for item in ctx.injections] == ["active"]
    assert ctx.require("late") == 1
    assert_steady(ctx)

    await ctx.retract("store")
    assert log == ["start", "stop"]
    assert [item.status for item in ctx.injections] == ["pending"]
    assert "late" not in ctx.services


async def test_inject_restarts_after_dependency_returns(ctx: Context) -> None:
    runs: list[int] = []
    await ctx.inject(["dep"], lambda c: runs.append(1))
    await ctx.provide("dep", 1)
    await ctx.retract("dep")
    await ctx.provide("dep", 1)
    assert runs == [1, 1]
    assert [item.status for item in ctx.injections] == ["active"]


async def test_inject_callback_failure_is_marked_and_retried_after_dep_cycle(
    ctx: Context,
) -> None:
    calls: list[int] = []

    async def callback(c: Context) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("cb boom")
        await c.provide("cb-out", 1)

    await ctx.inject(["dep"], callback)
    with pytest.raises(RuntimeError, match="cb boom"):
        await ctx.provide("dep", 1)

    record = ctx.injections[0]
    assert record.status == "failed"
    assert record.error is not None

    await ctx.retract("dep")  # 依赖走了：回 pending，不原地重试
    assert [item.status for item in ctx.injections] == ["pending"]

    await ctx.provide("dep", 1)  # 依赖回来：重试成功
    assert [item.status for item in ctx.injections] == ["active"]
    assert ctx.require("cb-out") == 1
    assert len(calls) == 2


async def test_inject_handle_cancels_subscription(ctx: Context) -> None:
    log: list[str] = []
    handle = await ctx.inject(
        ["dep"],
        lambda c: (log.append("start"), lambda: log.append("stop"))[1],
    )
    await ctx.provide("dep", 1)
    assert log == ["start"]

    await handle()
    await handle()  # 幂等
    assert log == ["start", "stop"]
    assert ctx.injections == []

    await ctx.retract("dep")
    await ctx.provide("dep", 1)
    assert log == ["start", "stop"]  # 订阅已取消，不再重跑


async def test_inject_inside_effect_is_cancelled_with_it(ctx: Context) -> None:
    log: list[str] = []

    async def install() -> object:
        return await ctx.inject(
            ["dep"],
            lambda c: (log.append("start"), lambda: log.append("stop"))[1],
        )

    await ctx.effect("e", install)
    await ctx.provide("dep", 1)
    assert log == ["start"]

    await ctx.unload_effect("e")
    assert log == ["start", "stop"]
    assert ctx.injections == []


async def test_inject_provides_service_for_dependents(ctx: Context) -> None:
    user = make_plugin("user", inject=("late",))
    await load(ctx, user)
    assert info(ctx, "user").status == "pending"

    async def callback(c: Context) -> None:
        await c.provide("late", 1)

    await ctx.inject(["dep"], callback)
    await ctx.provide("dep", 1)
    assert info(ctx, "user").status == "active"
    assert_steady(ctx)

    await ctx.retract("dep")
    assert info(ctx, "user").status == "pending"
    assert_steady(ctx)


async def test_inject_validation(ctx: Context) -> None:
    with pytest.raises(TypeError):
        await ctx.inject("store", lambda c: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await ctx.inject([], lambda c: None)
    with pytest.raises(ValueError):
        await ctx.inject(["a", "a"], lambda c: None)
    with pytest.raises(ValueError):
        await ctx.inject(["a", ""], lambda c: None)
    with pytest.raises(TypeError):
        await ctx.inject(["a"], "not-callable")  # type: ignore[arg-type]


async def test_injections_snapshot_fields(ctx: Context) -> None:
    await ctx.inject(["a", "b"], lambda c: None)
    snapshot = ctx.injections
    assert snapshot[0].name == "inject:1"
    assert snapshot[0].deps == ("a", "b")
    assert snapshot[0].missing == ("a", "b")
    assert snapshot[0].status == "pending"


async def test_inject_mid_unwind_does_not_reactivate_deactivated_plugin(
    ctx: Context,
) -> None:
    """注入订阅的清理发生在插件 effect 回卷途中，不能把退位中的插件又拉起来。"""
    order: list[str] = []

    class Provider:
        name = "provider"

        async def setup(self, c: Context) -> object:
            await c.provide("p-out", 1)
            return lambda: order.append("provider-clean")

    class Subscriber:
        name = "subscriber"

        async def setup(self, c: Context) -> object:
            async def start(cc: Context) -> None:
                await cc.provide("sub-out", 1)

            await c.inject(["dep"], start)
            return lambda: order.append("subscriber-clean")

    await load(ctx, Provider(), Subscriber())
    await ctx.provide("dep", 1)
    assert info(ctx, "subscriber").status == "active"

    await ctx.unload_plugin("subscriber")
    assert all(item.name != "subscriber" for item in ctx.plugins)
    assert "sub-out" not in ctx.services
    assert_steady(ctx)


async def test_inject_rejects_non_iterable_deps(ctx: Context) -> None:
    with pytest.raises(TypeError):
        await ctx.inject(42, lambda c: None)  # type: ignore[arg-type]
