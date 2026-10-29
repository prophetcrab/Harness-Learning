"""变更串行化：revision 与 last_change 的口径。"""

from __future__ import annotations

import pytest
from support import info, make_plugin

from core import (
    Context,
    DuplicatePluginError,
    DuplicateSlotError,
    MissingEffectError,
    MissingServiceError,
)


async def test_revision_starts_at_zero_and_counts_effective_changes(ctx: Context) -> None:
    assert ctx.revision == 0
    assert ctx.last_change is None

    await ctx.provide("a", 1)
    assert ctx.revision == 1

    await ctx.provide("b", 2)
    assert ctx.revision == 2

    await ctx.retract("a")
    assert ctx.revision == 3


async def test_one_public_operation_is_one_revision(ctx: Context) -> None:
    class P:
        name = "p"
        inject = ("a",)

        async def setup(self, c: Context) -> None:
            await c.provide("p-out", 1)
            await c.effect("p-child", lambda: None)
            c.on("evt", lambda **kw: None)

    await ctx.provide("a", 1)
    before = ctx.revision
    await ctx.load_plugin(P())
    assert ctx.revision - before == 1
    assert ctx.last_change is not None
    assert ctx.last_change.activated == ("p",)


async def test_failed_rollback_does_not_bump_revision(ctx: Context) -> None:
    async def bad() -> None:
        await ctx.provide("half", 1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await ctx.effect("bad", bad)
    assert ctx.revision == 0


async def test_duplicate_and_missing_ops_do_not_bump_revision(ctx: Context) -> None:
    await ctx.provide("a", 1)
    await ctx.load_plugin(make_plugin("p"))
    before = ctx.revision

    with pytest.raises(DuplicateSlotError):
        await ctx.provide("a", 2)
    with pytest.raises(MissingServiceError):
        await ctx.retract("nope")
    with pytest.raises(MissingEffectError):
        await ctx.unload_effect("nope")
    with pytest.raises(DuplicatePluginError):
        await ctx.load_plugin(make_plugin("p"))
    assert ctx.revision == before


async def test_last_change_reports_activated_and_retired(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("user", inject=("s",)))  # 先 pending
    await ctx.load_plugin(make_plugin("provider", provides=("s",)))

    last = ctx.last_change
    assert last is not None
    assert last.activated == ("provider", "user")

    await ctx.retract("s")
    last = ctx.last_change
    assert last is not None
    assert last.retired == ("user",)


async def test_cleanup_failure_still_bumps_revision(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("p", cleanup_error=RuntimeError("clean boom")))
    before = ctx.revision
    with pytest.raises(RuntimeError, match="clean boom"):
        await ctx.unload_plugin("p")
    assert ctx.revision == before + 1


async def test_injection_started_and_stopped_are_reported(ctx: Context) -> None:
    await ctx.inject(["dep"], lambda c: None)
    await ctx.provide("dep", 1)
    last = ctx.last_change
    assert last is not None
    assert last.injections_started == ("inject:1",)

    await ctx.retract("dep")
    last = ctx.last_change
    assert last is not None
    assert last.injections_stopped == ("inject:1",)


async def test_revision_advances_on_plugin_state_and_effect_tables(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("p", inject=("s",)))  # pending 也算变更
    assert ctx.revision == 1
    await ctx.provide("s", 1)  # p 被激活
    assert ctx.revision == 2
    await ctx.disable_plugin("p")
    assert ctx.revision == 3
    await ctx.enable_plugin("p")
    assert ctx.revision == 4
    assert info(ctx, "p").status == "active"
