"""事件：广播 / 瀑布 / 订阅生命周期与派发快照。"""

from __future__ import annotations

import pytest
from support import make_plugin

from core import Context


async def test_emit_delivers_in_registration_order(ctx: Context) -> None:
    seen: list[str] = []
    ctx.on("e", lambda **kw: seen.append(f"a:{kw['n']}"))
    ctx.on("e", lambda **kw: seen.append(f"b:{kw['n']}"))
    await ctx.emit("e", n=1)
    assert seen == ["a:1", "b:1"]


async def test_emit_without_subscribers_is_noop(ctx: Context) -> None:
    await ctx.emit("nobody")


async def test_emit_handler_error_propagates_and_stops_later_handlers(ctx: Context) -> None:
    seen: list[str] = []

    def bad(**kw: object) -> None:
        seen.append("bad")
        raise RuntimeError("boom")

    ctx.on("e", lambda **kw: seen.append("first"))
    ctx.on("e", bad)
    ctx.on("e", lambda **kw: seen.append("third"))

    with pytest.raises(RuntimeError, match="boom"):
        await ctx.emit("e")
    assert seen == ["first", "bad"]


async def test_waterfall_chain_replaces_value(ctx: Context) -> None:
    ctx.on("w", lambda value, **kw: value * 2)
    ctx.on("w", lambda value, **kw: None)  # 不干预
    ctx.on("w", lambda value, **kw: value + 1)
    assert await ctx.waterfall("w", 3) == 7


async def test_waterfall_can_wrap_a_callable(ctx: Context) -> None:
    calls: list[str] = []

    def wrap(value, **kw):
        def inner(*args, **kwargs):
            calls.append("before")
            result = value(*args, **kwargs)
            calls.append("after")
            return result

        return inner

    ctx.on("exec", wrap)

    def action() -> str:
        calls.append("action")
        return "ok"

    wrapped = await ctx.waterfall("exec", action)
    assert wrapped() == "ok"
    assert calls == ["before", "action", "after"]


async def test_dispatch_uses_snapshot(ctx: Context) -> None:
    seen: list[str] = []

    def late(**kw: object) -> None:
        seen.append("late")

    def subscribe_during_dispatch(**kw: object) -> None:
        seen.append("first")
        ctx.on("e", late)

    ctx.on("e", subscribe_during_dispatch)
    await ctx.emit("e")
    assert seen == ["first"]  # 本轮不派发新订阅

    await ctx.emit("e")
    assert seen == ["first", "first", "late"]


async def test_unsubscribe_during_dispatch_takes_effect_next_round(ctx: Context) -> None:
    seen: list[str] = []

    def second(**kw: object) -> None:
        seen.append("second")

    def first(**kw: object) -> None:
        seen.append("first")
        off()

    off = ctx.on("e", second)
    ctx.on("e", first)  # 先注册 second，再注册 first：本轮顺序 second、first

    await ctx.emit("e")
    assert seen == ["second", "first"]
    await ctx.emit("e")
    assert seen == ["second", "first", "first"]


async def test_off_is_idempotent(ctx: Context) -> None:
    calls: list[int] = []
    off = ctx.on("e", lambda **kw: calls.append(1))
    off()
    off()
    await ctx.emit("e")
    assert calls == []


async def test_effect_unload_revokes_subscription(ctx: Context) -> None:
    calls: list[int] = []
    await ctx.effect("e", lambda: ctx.on("evt", lambda **kw: calls.append(1)))
    await ctx.emit("evt")

    await ctx.unload_effect("e")
    await ctx.emit("evt")
    assert calls == [1]
    assert ctx.event_names == []


async def test_subscription_inside_plugin_is_revoked_on_unload(ctx: Context) -> None:
    calls: list[int] = []

    class Sub:
        name = "sub"

        def setup(self, c: Context) -> None:
            c.on("evt", lambda **kw: calls.append(1))

    await ctx.load_plugin(Sub())
    await ctx.emit("evt")
    await ctx.unload_plugin("sub")
    await ctx.emit("evt")
    assert calls == [1]


def test_on_validation(ctx: Context) -> None:
    with pytest.raises(ValueError):
        ctx.on("", lambda **kw: None)
    with pytest.raises(TypeError):
        ctx.on("e", "not-callable")  # type: ignore[arg-type]


def test_event_names_and_cleanup(ctx: Context) -> None:
    off_a = ctx.on("a", lambda **kw: None)
    ctx.on("b", lambda **kw: None)
    assert ctx.event_names == ["a", "b"]
    off_a()
    assert ctx.event_names == ["b"]


async def test_plugin_events_survive_unrelated_plugin_changes(ctx: Context) -> None:
    calls: list[str] = []

    class Listener:
        name = "listener"

        def setup(self, c: Context) -> None:
            c.on("msg", lambda **kw: calls.append(kw["text"]))

    await ctx.load_plugin(Listener())
    await ctx.load_plugin(make_plugin("other", provides=("x",)))
    await ctx.unload_plugin("other")
    await ctx.emit("msg", text="hi")
    assert calls == ["hi"]
