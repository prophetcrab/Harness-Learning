"""步 4 验收：事件（on / emit / waterfall）。

对应 PLAN.md「M1 拆步」第 4 步：广播与瀑布；退订随 effect 回卷；
派发名单在派发开始时取快照（半途订阅/退订从下一轮生效）。

事件是插件之间的**第二条通道**（第一条是服务）：服务用来"用别人的能力"，
事件用来"观察与拦截"——waterfall 的中间件语义就是策略插座的本体。
"""

from __future__ import annotations

import pytest

from core import Context

# ---------------------------------------------------------------------------
# 广播（emit）
# ---------------------------------------------------------------------------


def test_on_emit_roundtrip():
    ctx = Context()
    got: list[str] = []
    ctx.on("hello", lambda who: got.append(who))
    ctx.emit("hello", who="世界")
    assert got == ["世界"]


def test_handlers_run_in_registration_order():
    ctx = Context()
    order: list[str] = []
    ctx.on("evt", lambda: order.append("a"))
    ctx.on("evt", lambda: order.append("b"))
    ctx.emit("evt")
    assert order == ["a", "b"]


def test_unsubscribe_stops_delivery_and_is_idempotent():
    ctx = Context()
    got: list[str] = []
    off = ctx.on("evt", lambda: got.append("x"))
    ctx.emit("evt")
    off()
    off()  # 幂等：退过一次再退不算错
    ctx.emit("evt")
    assert got == ["x"]
    assert ctx.event_names == []


def test_emit_without_subscribers_is_fine():
    ctx = Context()
    ctx.emit("nobody-listens", text="hi")  # 广播给空房间：不报错


def test_handler_error_propagates_and_stops_dispatch():
    """fail loud：handler 抛错直接往外抛，快照里的后续 handler 不再执行。"""
    ctx = Context()
    got: list[str] = []

    def boom():
        raise RuntimeError("handler 炸了")

    ctx.on("evt", lambda: got.append("first"))
    ctx.on("evt", boom)
    ctx.on("evt", lambda: got.append("third"))
    with pytest.raises(RuntimeError, match="handler 炸了"):
        ctx.emit("evt")
    assert got == ["first"]


def test_subscription_during_dispatch_takes_effect_next_round():
    ctx = Context()
    got: list[str] = []

    def late():
        got.append("late")

    def first():
        got.append("first")
        ctx.on("evt", late)

    ctx.on("evt", first)
    ctx.emit("evt")
    assert got == ["first"]  # 本轮名单在派发开始时已取定
    ctx.emit("evt")
    assert got == ["first", "first", "late"]


def test_unsubscribe_during_dispatch_does_not_break_the_round():
    ctx = Context()
    got: list[str] = []
    third_ran: list[bool] = []

    def second():
        got.append("second")
        off_third()  # 派发途中退订别人

    def third():
        third_ran.append(True)

    ctx.on("evt", lambda: got.append("first"))
    ctx.on("evt", second)
    off_third = ctx.on("evt", third)

    ctx.emit("evt")
    assert got == ["first", "second"]
    assert third_ran == [True]  # 本轮名单已定：照常派发
    ctx.emit("evt")
    assert third_ran == [True]  # 下一轮起不在名单里


def test_event_names_lists_only_live_subscriptions():
    ctx = Context()
    off = ctx.on("a", lambda: None)
    ctx.on("b", lambda: None)
    assert ctx.event_names == ["a", "b"]
    off()
    assert ctx.event_names == ["b"]


def test_invalid_subscription_fails_loud():
    ctx = Context()
    with pytest.raises(ValueError):
        ctx.on("", lambda: None)
    with pytest.raises(TypeError):
        ctx.on("evt", "不是函数")


# ---------------------------------------------------------------------------
# 瀑布（waterfall）：链式加工 / 策略插座
# ---------------------------------------------------------------------------


def test_waterfall_passes_value_through_chain():
    ctx = Context()
    ctx.on("count/step", lambda value: value + 1)
    ctx.on("count/step", lambda value: value + 1)
    assert ctx.waterfall("count/step", 0) == 2


def test_waterfall_later_handler_sees_replaced_value():
    ctx = Context()
    seen: list[tuple[str, str]] = []

    def first(value):
        seen.append(("first", value))
        return value + "→first"

    def second(value):
        seen.append(("second", value))
        return value + "→second"

    ctx.on("chain", first)
    ctx.on("chain", second)
    assert ctx.waterfall("chain", "start") == "start→first→second"
    assert seen == [("first", "start"), ("second", "start→first")]


def test_waterfall_none_means_no_change():
    ctx = Context()
    ctx.on("chain", lambda value: None)
    ctx.on("chain", lambda value: "改过的值")
    assert ctx.waterfall("chain", "原值") == "改过的值"
    # 没有 handler 返回非 None 时，原值原样返回
    empty = Context()
    assert empty.waterfall("chain", "原值") == "原值"


def test_waterfall_context_kwargs_visible():
    ctx = Context()
    seen: dict = {}

    def handler(value, **context):
        seen.update(context)
        return value

    ctx.on("chain", handler)
    ctx.waterfall("chain", 1, session="s1", step=3)
    assert seen == {"session": "s1", "step": 3}


def test_waterfall_error_propagates():
    ctx = Context()
    ctx.on("chain", lambda value: (_ for _ in ()).throw(ValueError("链路里炸了")))
    with pytest.raises(ValueError, match="链路里炸了"):
        ctx.waterfall("chain", 1)


# ---------------------------------------------------------------------------
# 与 effect 回卷的配合：订阅随装载期一起消失
# ---------------------------------------------------------------------------


def test_subscription_inside_effect_is_auto_unsubscribed():
    ctx = Context()
    got: list[int] = []
    ctx.effect("listen", lambda: ctx.on("evt", lambda: got.append(1)))
    ctx.emit("evt")
    assert got == [1]

    ctx.unload_effect("listen")
    ctx.emit("evt")
    assert got == [1]  # 没有第二份
    assert ctx.event_names == []


def test_plugin_unload_takes_subscriptions_with_it():
    got: list[str] = []

    class Listener:
        name = "listener"

        def setup(self, ctx):
            ctx.on("evt", lambda: got.append("heard"))

    ctx = Context()
    ctx.load_plugin(Listener())
    ctx.emit("evt")
    assert got == ["heard"]

    ctx.unload_plugin("listener")
    ctx.emit("evt")
    assert got == ["heard"]  # 订阅随插件一起回卷
    assert ctx.event_names == []


def test_disable_then_enable_resubscribes():
    got: list[str] = []

    class Listener:
        name = "listener"

        def setup(self, ctx):
            ctx.on("evt", lambda: got.append("heard"))

    ctx = Context()
    ctx.load_plugin(Listener())
    ctx.disable_plugin("listener")
    ctx.emit("evt")
    assert got == []  # 停用后订阅已回卷
    ctx.enable_plugin("listener")
    ctx.emit("evt")
    assert got == ["heard"]


def test_failed_setup_rolls_back_subscriptions():
    class Bad:
        name = "bad"

        def setup(self, ctx):
            ctx.on("evt", lambda: None)
            raise RuntimeError("boom")

    ctx = Context()
    with pytest.raises(RuntimeError, match="boom"):
        ctx.load_plugin(Bad())
    assert ctx.event_names == []  # 失败不留半装：订阅也一起回卷
