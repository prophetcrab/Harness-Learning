"""稳定化：退位顺序、清理失败后的收敛、循环依赖、激活失败。"""

from __future__ import annotations

import pytest
from support import assert_steady, info, load

from core import Context


async def test_cleanup_failure_still_leaves_world_steady(ctx: Context) -> None:
    """bug 修复：退位途中清理抛错，也要把其余依赖者退完，再抛第一个异常。"""

    class Bad:
        name = "bad"
        inject = ("root",)

        async def setup(self, c: Context) -> object:
            await c.provide("bad-out", 1)
            return lambda: (_ for _ in ()).throw(RuntimeError("bad clean"))

    class Leaf:
        name = "leaf"
        inject = ("bad-out",)

        async def setup(self, c: Context) -> None:
            await c.provide("leaf-out", 1)

    await ctx.provide("root", 1)
    await ctx.load_plugin(Bad())
    await ctx.load_plugin(Leaf())

    with pytest.raises(RuntimeError, match="bad clean"):
        await ctx.retract("root")

    assert ctx.active_plugins == []
    assert info(ctx, "bad").status == "pending"
    assert info(ctx, "leaf").status == "pending"
    assert_steady(ctx)


async def test_cleanup_errors_do_not_stop_other_cleanups(ctx: Context) -> None:
    cleaned: list[str] = []

    class Bad:
        name = "bad"
        inject = ("root",)

        def setup(self, c: Context) -> object:
            return lambda: (cleaned.append("bad"), (_ for _ in ()).throw(RuntimeError("x")))[0]

    class Good:
        name = "good"
        inject = ("root",)

        def setup(self, c: Context) -> object:
            return lambda: cleaned.append("good")

    await ctx.provide("root", 1)
    await load(ctx, Bad(), Good())

    with pytest.raises(RuntimeError):
        await ctx.retract("root")

    assert set(cleaned) == {"bad", "good"}
    assert ctx.active_plugins == []
    assert_steady(ctx)


async def test_effect_unload_cascade_is_deep_first(ctx: Context) -> None:
    order: list[str] = []

    class A:
        name = "a"
        inject = ("s",)

        async def setup(self, c: Context) -> object:
            await c.provide("a-out", 1)
            return lambda: order.append("a")

    class B:
        name = "b"
        inject = ("a-out",)

        def setup(self, c: Context) -> object:
            return lambda: order.append("b")

    await ctx.load_plugin(A())
    await ctx.load_plugin(B())

    async def install() -> None:
        await ctx.provide("s", 1)

    handle = await ctx.effect("provider", install)
    assert ctx.active_plugins == ["a", "b"]

    await handle()
    assert order == ["b", "a"]
    assert_steady(ctx)


async def test_activation_failure_keeps_world_steady(ctx: Context) -> None:
    class Broken:
        name = "broken"
        inject = ("s",)

        def setup(self, c: Context) -> None:
            raise RuntimeError("setup boom")

    await ctx.load_plugin(Broken())
    with pytest.raises(RuntimeError, match="setup boom"):
        await ctx.provide("s", 1)

    assert info(ctx, "broken").status == "failed"
    assert_steady(ctx)


async def test_circular_dependency_stays_pending_and_terminates(ctx: Context) -> None:
    class A:
        name = "a"
        inject = ("b-out",)

        async def setup(self, c: Context) -> None:
            await c.provide("a-out", 1)

    class B:
        name = "b"
        inject = ("a-out",)

        async def setup(self, c: Context) -> None:
            await c.provide("b-out", 1)

    await ctx.load_plugin(A())
    await ctx.load_plugin(B())
    assert ctx.active_plugins == []
    assert set(ctx.pending_plugins) == {"a", "b"}
    assert_steady(ctx)


async def test_no_activation_while_effect_is_installing(ctx: Context) -> None:
    order: list[str] = []

    class Waiting:
        name = "waiting"
        inject = ("s",)

        def setup(self, c: Context) -> None:
            order.append("waiting-setup")

    await ctx.load_plugin(Waiting())

    async def install() -> None:
        order.append("install-begin")
        await ctx.provide("s", 1)
        order.append("install-end")

    await ctx.effect("e", install)
    assert order == ["install-begin", "install-end", "waiting-setup"]


async def test_suppressed_dependents_are_not_reactivated_during_cascade(
    ctx: Context,
) -> None:
    setups: list[int] = []

    class Provider:
        name = "provider"

        async def setup(self, c: Context) -> None:
            await c.provide("p-out", 1)

    class User:
        name = "user"
        inject = ("p-out",)

        def setup(self, c: Context) -> None:
            setups.append(1)

    await load(ctx, Provider(), User())
    assert setups == [1]

    await ctx.disable_plugin("provider")
    assert setups == [1]  # 级联途中没有把 user 误激活
    assert info(ctx, "user").status == "pending"
    assert_steady(ctx)


async def test_settle_finishes_other_activations_before_raising(ctx: Context) -> None:
    """一次 settle 里某个激活失败，依赖已齐的其它 pending 仍要被激活。"""

    class Fail:
        name = "fail"
        inject = ("g",)

        def setup(self, c: Context) -> None:
            raise RuntimeError("fail boom")

    class Ok:
        name = "ok"
        inject = ("g",)

        async def setup(self, c: Context) -> None:
            await c.provide("ok-out", 1)

    await ctx.load_plugin(Fail())
    await ctx.load_plugin(Ok())

    with pytest.raises(RuntimeError, match="fail boom"):
        await ctx.provide("g", 1)

    assert info(ctx, "fail").status == "failed"
    assert info(ctx, "ok").status == "active"
    assert_steady(ctx)


async def test_settle_finishes_other_injections_before_raising(ctx: Context) -> None:
    runs: list[int] = []

    await ctx.inject(["dep"], lambda c: (_ for _ in ()).throw(RuntimeError("inject boom")))
    await ctx.inject(["dep"], lambda c: runs.append(1))

    with pytest.raises(RuntimeError, match="inject boom"):
        await ctx.provide("dep", 1)

    assert [item.status for item in ctx.injections] == ["failed", "active"]
    assert runs == [1]
    assert_steady(ctx)


async def test_first_error_wins_and_others_become_notes() -> None:
    """同一次 settle 里既有清理异常又有激活失败：抛第一个，另一个进 note。"""

    class BadClean:
        name = "badclean"
        inject = ("a-root",)

        async def setup(self, c: Context) -> object:
            await c.provide("badclean-out", 1)
            return lambda: (_ for _ in ()).throw(RuntimeError("clean boom"))

    class BadSetup:
        name = "badsetup"
        inject = ("b-slot",)

        def setup(self, c: Context) -> None:
            raise RuntimeError("setup boom")

    ctx = Context()
    await ctx.provide("a-root", 1)
    await ctx.load_plugin(BadClean())
    await ctx.load_plugin(BadSetup())

    async def install() -> None:
        # 归到同一次 settle：一次撤依赖、一次补依赖，收尾时统一收敛
        await ctx.retract("a-root")
        await ctx.provide("b-slot", 1)

    with pytest.raises(RuntimeError, match="clean boom") as excinfo:
        await ctx.effect("trigger", install)

    notes = getattr(excinfo.value, "__notes__", [])
    assert any("setup boom" in note for note in notes)
    assert ctx.active_plugins == []
    assert_steady(ctx)


async def test_settle_is_deferred_until_unwind_finishes(ctx: Context) -> None:
    """disposer 运行期间不推送稳定态：清理里的 provide 要等整次卸载收尾才生效。"""
    order: list[str] = []

    class Provider:
        name = "provider"

        def setup(self, c: Context) -> object:
            async def cleanup() -> None:
                order.append("cleanup-begin")
                await c.provide("g", 1)
                order.append("cleanup-end")

            return cleanup

    class Waiting:
        name = "waiting"
        inject = ("g",)

        def setup(self, c: Context) -> None:
            order.append("waiting-setup")

    await ctx.load_plugin(Provider())
    await ctx.load_plugin(Waiting())
    await ctx.unload_plugin("provider")

    assert order == ["cleanup-begin", "cleanup-end", "waiting-setup"]
    assert_steady(ctx)


async def test_unload_cleanup_error_not_masked_by_settle_error(ctx: Context) -> None:
    """卸载自己的清理错误优先抛出；收尾 settle 的错误作为 note。"""

    class Provider:
        name = "provider"

        def setup(self, c: Context) -> object:
            async def cleanup() -> None:
                await c.provide("g", 1)
                raise RuntimeError("clean boom")

            return cleanup

    class Broken:
        name = "broken"
        inject = ("g",)

        def setup(self, c: Context) -> None:
            raise RuntimeError("setup boom")

    await ctx.load_plugin(Provider())
    await ctx.load_plugin(Broken())

    with pytest.raises(RuntimeError, match="clean boom") as excinfo:
        await ctx.unload_plugin("provider")

    notes = getattr(excinfo.value, "__notes__", [])
    assert any("setup boom" in note for note in notes)
    assert_steady(ctx)
