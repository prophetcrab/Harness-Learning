"""effect 回卷：嵌套顺序、失败回滚、句柄语义、清理异常。"""

from __future__ import annotations

import pytest
from support import assert_steady, info, load, make_plugin

from core import (
    Context,
    DuplicateEffectError,
    MissingEffectError,
)


async def test_nested_unwind_child_before_parent_and_lifo(ctx: Context) -> None:
    order: list[str] = []

    async def install() -> object:
        order.append("install")
        await ctx.effect(
            "child",
            lambda: order.append("child-install") or (lambda: order.append("child-clean")),
        )
        return lambda: order.append("self-clean")

    await ctx.effect("top", install)
    assert order == ["install", "child-install"]

    await ctx.unload_effect("top")
    assert order == ["install", "child-install", "child-clean", "self-clean"]
    assert ctx.effects == []


async def test_plugin_cleanup_runs_before_its_services_are_revoked(ctx: Context) -> None:
    seen: list[set[str]] = []

    class Own:
        name = "own"

        async def setup(self, c: Context) -> object:
            await c.provide("own-out", 1)
            return lambda: seen.append(set(c.services))

    await ctx.load_plugin(Own())
    await ctx.unload_plugin("own")
    assert "own-out" in seen[0]


async def test_failed_install_rolls_back_everything(ctx: Context) -> None:
    async def bad() -> object:
        await ctx.provide("half", 1)
        await ctx.effect("inner", lambda: None)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await ctx.effect("bad", bad)

    assert ctx.services == []
    assert ctx.effects == []
    assert ctx.revision == 0


async def test_failed_install_unwinds_nested_children(ctx: Context) -> None:
    cleaned: list[str] = []

    async def bad() -> object:
        await ctx.effect("inner", lambda: (lambda: cleaned.append("inner")))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await ctx.effect("outer", bad)
    assert cleaned == ["inner"]
    assert ctx.effects == []


async def test_install_returning_non_callable_is_rejected(ctx: Context) -> None:
    with pytest.raises(TypeError):
        await ctx.effect("bad", lambda: 42)
    assert ctx.effects == []


async def test_reserved_effect_names(ctx: Context) -> None:
    with pytest.raises(ValueError):
        await ctx.effect("plugin:x", lambda: None)
    with pytest.raises(ValueError):
        await ctx.effect("inject:x", lambda: None)
    with pytest.raises(ValueError):
        await ctx.effect("", lambda: None)


async def test_duplicate_effect_name_and_reuse_after_unload(ctx: Context) -> None:
    await ctx.effect("e", lambda: None)
    with pytest.raises(DuplicateEffectError):
        await ctx.effect("e", lambda: None)

    await ctx.unload_effect("e")
    await ctx.effect("e", lambda: None)  # 名字释放后可重用
    assert ctx.effects == ["e"]


async def test_effect_handle_only_affects_its_own_load(ctx: Context) -> None:
    old_handle = await ctx.effect("e", lambda: None)
    await ctx.unload_effect("e")

    await ctx.effect("e", lambda: None)
    await old_handle()  # 认"这一次装载"：旧句柄是空操作
    assert ctx.effects == ["e"]
    await old_handle()
    assert ctx.effects == ["e"]


async def test_effect_handle_is_idempotent(ctx: Context) -> None:
    cleaned: list[int] = []
    handle = await ctx.effect("e", lambda: (lambda: cleaned.append(1)))
    await handle()
    await handle()
    assert cleaned == [1]
    assert ctx.effects == []


async def test_unload_missing_effect_raises(ctx: Context) -> None:
    with pytest.raises(MissingEffectError):
        await ctx.unload_effect("nope")


async def test_plugin_effect_cannot_be_unloaded_directly(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("p"))
    with pytest.raises(ValueError):
        await ctx.unload_effect("plugin:p")
    assert info(ctx, "p").status == "active"


async def test_disposer_error_does_not_stop_other_unwinding(ctx: Context) -> None:
    order: list[str] = []

    def boom(name: str):
        def disposer() -> None:
            order.append(name)
            raise RuntimeError(name)

        return disposer

    async def install() -> None:
        await ctx.effect("first", lambda: (lambda: order.append("first")))
        await ctx.effect("second", lambda: boom("second"))
        await ctx.effect("third", lambda: (lambda: order.append("third")))

    await ctx.effect("top", install)

    with pytest.raises(RuntimeError, match="second"):
        await ctx.unload_effect("top")

    # 子 effect 同级后进先出：third -> second(炸) -> first，全部跑完
    assert order == ["third", "second", "first"]
    assert ctx.effects == []


async def test_child_unloaded_directly_is_not_unwound_twice(ctx: Context) -> None:
    cleaned: list[str] = []

    async def install() -> None:
        await ctx.effect("child", lambda: (lambda: cleaned.append("child")))

    await ctx.effect("parent", install)

    await ctx.unload_effect("child")
    assert cleaned == ["child"]
    await ctx.unload_effect("parent")
    assert cleaned == ["child"]


async def test_unload_effect_triggers_settle(ctx: Context) -> None:
    user = make_plugin("user", inject=("s",))
    await load(ctx, user)
    assert info(ctx, "user").status == "pending"

    async def install() -> None:
        await ctx.provide("s", 1)

    handle = await ctx.effect("e", install)
    assert info(ctx, "user").status == "active"

    await handle()
    assert info(ctx, "user").status == "pending"
    assert_steady(ctx)
