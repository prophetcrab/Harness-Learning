"""async-first 语义：异步 setup / effect / disposer / 事件 / inject / 并发 task。"""

from __future__ import annotations

import asyncio

import pytest
from support import assert_steady, load, make_plugin

from core import Context, DuplicatePluginError, DuplicateSlotError, MissingServiceError


async def test_async_setup_is_awaited(ctx: Context) -> None:
    order: list[str] = []

    class P:
        name = "p"

        async def setup(self, c: Context) -> None:
            order.append("setup-begin")
            await asyncio.sleep(0)
            order.append("setup-end")

    await ctx.load_plugin(P())
    assert order == ["setup-begin", "setup-end"]
    assert ctx.active_plugins == ["p"]


async def test_sync_setup_still_allowed(ctx: Context) -> None:
    order: list[str] = []

    class P:
        name = "p"

        def setup(self, c: Context) -> None:
            order.append("sync-setup")

    await ctx.load_plugin(P())
    assert order == ["sync-setup"]
    assert ctx.active_plugins == ["p"]


async def test_async_effect_install_and_disposer_are_awaited(ctx: Context) -> None:
    order: list[str] = []

    async def install() -> object:
        order.append("install-begin")
        await asyncio.sleep(0)
        order.append("install-end")

        async def cleanup() -> None:
            order.append("cleanup-begin")
            await asyncio.sleep(0)
            order.append("cleanup-end")

        return cleanup

    await ctx.effect("e", install)
    assert order == ["install-begin", "install-end"]

    await ctx.unload_effect("e")
    assert order == [
        "install-begin",
        "install-end",
        "cleanup-begin",
        "cleanup-end",
    ]


async def test_async_disposer_error_is_surfaced(ctx: Context) -> None:
    order: list[str] = []

    async def good() -> None:
        order.append("good")

    async def bad() -> None:
        order.append("bad")
        raise RuntimeError("async clean boom")

    async def install() -> None:
        await ctx.effect("first", lambda: good)
        await ctx.effect("second", lambda: bad)

    await ctx.effect("parent", install)

    with pytest.raises(RuntimeError, match="async clean boom"):
        await ctx.unload_effect("parent")
    # 子 effect 同级后进先出：second(bad) 先跑，first(good) 仍跑完
    assert order == ["bad", "good"]
    assert ctx.effects == []


async def test_async_install_failure_rolls_back(ctx: Context) -> None:
    async def bad() -> None:
        await ctx.provide("half", 1)
        raise RuntimeError("async install boom")

    with pytest.raises(RuntimeError, match="async install boom"):
        await ctx.effect("bad", bad)
    assert ctx.services == []
    assert ctx.effects == []
    assert ctx.revision == 0


async def test_async_event_handler_and_waterfall_are_awaited(ctx: Context) -> None:
    seen: list[int] = []

    async def handler(**kw: object) -> None:
        await asyncio.sleep(0)
        seen.append(int(kw["n"]))  # type: ignore[arg-type]

    ctx.on("evt", handler)
    await ctx.emit("evt", n=1)
    assert seen == [1]

    async def double(value: int, **kw: object) -> int:
        await asyncio.sleep(0)
        return value * 2

    ctx.on("w", double)
    assert await ctx.waterfall("w", 3) == 6


async def test_async_event_handler_error_propagates(ctx: Context) -> None:
    async def bad(**kw: object) -> None:
        raise RuntimeError("async handler boom")

    ctx.on("e", bad)
    with pytest.raises(RuntimeError, match="async handler boom"):
        await ctx.emit("e")


async def test_async_inject_callback_and_stop_disposer(ctx: Context) -> None:
    log: list[str] = []

    async def callback(c: Context) -> object:
        log.append("start")
        await c.provide("late", 1)

        async def stop() -> None:
            await asyncio.sleep(0)
            log.append("stop")

        return stop

    handle = await ctx.inject(["dep"], callback)
    await ctx.provide("dep", 1)
    assert log == ["start"]
    assert ctx.require("late") == 1

    await handle()
    assert log == ["start", "stop"]
    assert "late" not in ctx.services


async def test_async_inject_callback_failure_is_marked(ctx: Context) -> None:
    async def bad(c: Context) -> None:
        raise RuntimeError("async inject boom")

    await ctx.inject(["dep"], bad)
    with pytest.raises(RuntimeError, match="async inject boom"):
        await ctx.provide("dep", 1)
    assert [item.status for item in ctx.injections] == ["failed"]


async def test_async_cleanup_awaited_on_disable_and_unload(ctx: Context) -> None:
    log: list[str] = []

    class P:
        name = "p"

        async def setup(self, c: Context) -> object:
            await c.provide("p-out", 1)

            async def cleanup() -> None:
                await asyncio.sleep(0)
                log.append("clean")

            return cleanup

    await ctx.load_plugin(P())
    await ctx.disable_plugin("p")
    assert log == ["clean"]
    await ctx.enable_plugin("p")
    await ctx.unload_plugin("p")
    assert log == ["clean", "clean"]
    assert_steady(ctx)


async def test_setup_can_await_load_plugin_reentrantly(ctx: Context) -> None:
    log: list[str] = []

    class Guest:
        name = "guest"

        async def setup(self, c: Context) -> None:
            log.append("guest")

    class Host:
        name = "host"

        async def setup(self, c: Context) -> None:
            await c.load_plugin(Guest())
            log.append("host")

    await ctx.load_plugin(Host())
    assert ctx.active_plugins == ["host", "guest"]
    assert log == ["host", "guest"]
    assert_steady(ctx)


async def test_concurrent_same_name_load_has_one_winner() -> None:
    for _ in range(10):
        ctx = Context()
        barrier = asyncio.Barrier(4)
        results: list[str] = []

        class P:
            name = "same"

            async def setup(self, c: Context) -> None:
                await c.provide("s", 1)

        async def load_one(
            barrier: asyncio.Barrier = barrier,
            ctx: Context = ctx,
            results: list[str] = results,
        ) -> None:
            await barrier.wait()
            try:
                await ctx.load_plugin(P())
                results.append("ok")
            except DuplicatePluginError:
                results.append("dup")

        await asyncio.gather(*(load_one() for _ in range(4)))

        assert sorted(results) == ["dup", "dup", "dup", "ok"]
        assert len(ctx.plugins) == 1
        assert ctx.require("s") == 1
        assert_steady(ctx)


async def test_concurrent_load_unload_distinct_plugins(ctx: Context) -> None:
    async def worker(tid: int) -> None:
        for _ in range(25):
            name = f"t{tid}"
            await ctx.load_plugin(
                make_plugin(name, provides=(f"s{tid}",), log=[])
            )
            await ctx.unload_plugin(name)

    await asyncio.gather(*(worker(tid) for tid in range(4)))
    assert ctx.plugins == []
    assert ctx.services == []
    assert_steady(ctx)


async def test_concurrent_provide_retract_same_slot(ctx: Context) -> None:
    async def provider() -> None:
        for index in range(100):
            try:
                await ctx.provide("k", index)
            except DuplicateSlotError:
                pass

    async def retractor() -> None:
        for _ in range(100):
            try:
                await ctx.retract("k")
            except MissingServiceError:
                pass

    await asyncio.gather(provider(), retractor())
    assert len(ctx.services) <= 1
    if "k" in ctx.services:
        await ctx.retract("k")
    assert ctx.services == []


async def test_loads_are_serialized_across_tasks(ctx: Context) -> None:
    """公共操作仍串行：并发 load 之后台账/服务表一致，不会泄漏中间态。"""
    seen: list[tuple[int, tuple[str, ...]]] = []

    async def loader(index: int) -> None:
        await load(ctx, make_plugin(f"p{index}", provides=(f"s{index}",)))
        seen.append((index, tuple(sorted(ctx.services))))

    await asyncio.gather(*(loader(index) for index in range(5)))
    assert len(ctx.plugins) == 5
    assert len(ctx.services) == 5
    assert_steady(ctx)
