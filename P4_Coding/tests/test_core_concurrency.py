"""并发：公共操作串行化后的台账一致性（asyncio 单事件循环）。"""

from __future__ import annotations

import asyncio

from support import assert_steady, make_plugin

from core import Context, DuplicatePluginError, DuplicateSlotError, MissingServiceError


async def _load_same(ctx: Context, barrier: asyncio.Barrier, results: list[str]) -> None:
    await barrier.wait()
    try:
        await ctx.load_plugin(make_plugin("same", provides=("s",)))
        results.append("ok")
    except DuplicatePluginError:
        results.append("dup")


async def test_concurrent_same_name_load_has_one_winner() -> None:
    for _ in range(20):
        ctx = Context()
        barrier = asyncio.Barrier(2)
        results: list[str] = []

        await asyncio.gather(
            _load_same(ctx, barrier, results),
            _load_same(ctx, barrier, results),
        )

        assert sorted(results) == ["dup", "ok"]
        assert len(ctx.plugins) == 1
        assert ctx.require("s") == "same"
        assert_steady(ctx)


async def _load_unload_distinct(
    ctx: Context, tid: int, errors: list[BaseException]
) -> None:
    try:
        for _ in range(25):
            name = f"t{tid}"
            await ctx.load_plugin(make_plugin(name, provides=(f"s{tid}",)))
            await ctx.unload_plugin(name)
    except BaseException as exc:  # 收齐后统一断言
        errors.append(exc)


async def test_concurrent_load_unload_distinct_plugins() -> None:
    ctx = Context()
    errors: list[BaseException] = []

    await asyncio.gather(
        *(_load_unload_distinct(ctx, tid, errors) for tid in range(4))
    )

    assert errors == []
    assert ctx.plugins == []
    assert ctx.services == []
    assert_steady(ctx)


async def _provide_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for index in range(100):
            try:
                await ctx.provide("k", index)
            except DuplicateSlotError:
                pass
    except BaseException as exc:
        errors.append(exc)


async def _retract_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for _ in range(100):
            try:
                await ctx.retract("k")
            except MissingServiceError:
                pass
    except BaseException as exc:
        errors.append(exc)


async def test_concurrent_provide_and_retract_same_slot() -> None:
    ctx = Context()
    errors: list[BaseException] = []

    await asyncio.gather(
        _provide_loop(ctx, errors),
        _retract_loop(ctx, errors),
    )

    assert errors == []
    assert len(ctx.services) <= 1
    if "k" in ctx.services:
        await ctx.retract("k")
    assert ctx.services == []
    assert_steady(ctx)


async def _toggle_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for _ in range(60):
            try:
                await ctx.disable_plugin("p")
            except ValueError:
                pass
            try:
                await ctx.enable_plugin("p")
            except ValueError:
                pass
    except BaseException as exc:
        errors.append(exc)


async def test_concurrent_disable_enable_same_plugin() -> None:
    ctx = Context()
    await ctx.load_plugin(make_plugin("p", provides=("p-out",)))
    errors: list[BaseException] = []

    await asyncio.gather(*(_toggle_loop(ctx, errors) for _ in range(3)))

    assert errors == []
    assert ctx.active_plugins == ["p"]  # 最后一次是 enable
    assert_steady(ctx)


async def _subscribe_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for _ in range(100):
            off = ctx.on("evt", lambda **kw: None)
            off()
    except BaseException as exc:
        errors.append(exc)


async def _emit_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for _ in range(100):
            await ctx.emit("evt", n=1)
    except BaseException as exc:
        errors.append(exc)


async def test_concurrent_event_subscribe_and_emit() -> None:
    ctx = Context()
    errors: list[BaseException] = []

    await asyncio.gather(
        _subscribe_loop(ctx, errors),
        _subscribe_loop(ctx, errors),
        _emit_loop(ctx, errors),
        _emit_loop(ctx, errors),
    )

    assert errors == []
    assert ctx.event_names == []


async def _provide_retract_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for index in range(300):
            try:
                await ctx.provide("k", index)
            except DuplicateSlotError:
                pass
            try:
                await ctx.retract("k")
            except MissingServiceError:
                pass
    except BaseException as exc:
        errors.append(exc)


async def _require_loop(ctx: Context, errors: list[BaseException]) -> None:
    try:
        for _ in range(600):
            try:
                ctx.require("k")
            except MissingServiceError:
                pass
    except BaseException as exc:
        errors.append(exc)


async def test_require_never_leaks_key_error_while_slot_disappears() -> None:
    ctx = Context()
    errors: list[BaseException] = []

    await asyncio.gather(
        _provide_retract_loop(ctx, errors),
        _provide_retract_loop(ctx, errors),
        _require_loop(ctx, errors),
        _require_loop(ctx, errors),
    )

    assert errors == []
