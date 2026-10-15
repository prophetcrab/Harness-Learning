"""P3 基础包的内核与服务测试：provide / require / retract / 事件 / effect。"""

from __future__ import annotations

import pytest

from kernel import Context, DuplicateSlotError, UnknownEffectError

# ---------------------------------------------------------------------------
# 服务：命名单槽，重复 fail loud，retract 触发依赖重估
# ---------------------------------------------------------------------------


def test_provide_require_retract_roundtrip():
    ctx = Context()
    ctx.provide("greeting", "你好")
    assert ctx.require("greeting") == "你好"
    assert ctx.services == ["greeting"]
    assert ctx.retract("greeting") == "你好"
    assert ctx.services == []
    assert ctx.get("greeting") is None
    assert ctx.get("greeting", "默认") == "默认"


def test_duplicate_provide_fails_loud():
    ctx = Context()
    ctx.provide("fs", object())
    with pytest.raises(DuplicateSlotError):
        ctx.provide("fs", object())


def test_require_missing_fails_loud():
    ctx = Context()
    with pytest.raises(KeyError):
        ctx.require("nothing")


def test_retract_missing_fails_loud():
    ctx = Context()
    with pytest.raises(KeyError):
        ctx.retract("nothing")


# ---------------------------------------------------------------------------
# effect：注册即回卷；子先父后；故障不留半装状态
# ---------------------------------------------------------------------------


def test_effect_disposer_runs_and_slot_auto_rolled_back():
    ctx = Context()
    log: list[str] = []

    def install():
        ctx.provide("x", 1)
        log.append("install")
        return lambda: log.append("explicit-dispose")

    dispose = ctx.effect("e1", install)
    assert ctx.require("x") == 1
    dispose()
    assert ctx.services == []  # provide 的自动撤销生效
    assert log == ["install", "explicit-dispose"]  # LIFO：显式 disposer 先跑
    assert ctx.effect_names == []


def test_nested_effects_unwind_children_first():
    ctx = Context()
    order: list[str] = []

    def outer_install():
        order.append("install-outer")
        ctx.effect("inner", lambda: (order.append("install-inner"), None)[1])
        return lambda: order.append("dispose-outer")

    ctx.effect("outer", outer_install)
    ctx.unload("outer")
    assert order == ["install-outer", "install-inner", "dispose-outer"]


def test_effect_load_failure_leaves_no_half_state():
    ctx = Context()

    def install():
        ctx.provide("half", True)
        raise ValueError("装载崩了")

    with pytest.raises(ValueError):
        ctx.effect("broken", install)
    assert ctx.services == []  # 已 provide 的槽位被回卷
    assert ctx.effect_names == []


def test_disposer_error_does_not_stop_other_rollbacks():
    ctx = Context()
    order: list[str] = []

    def boom_install():
        def boom() -> None:
            raise RuntimeError("disposer 炸了")

        return boom

    def install():
        ctx.register(lambda: order.append("first"))  # 子 effect：回卷时打印
        ctx.register(boom_install)  # 子 effect：它的 disposer 会抛
        return lambda: order.append("outer")

    ctx.effect("e", install)
    with pytest.raises(RuntimeError):
        ctx.unload("e")
    # 一个 disposer 抛错不影响其余回卷：另一个子 effect 与父 disposer 都跑到了。
    assert order == ["first", "outer"]
    assert ctx.effect_names == []


def test_unload_unknown_effect_fails_loud():
    ctx = Context()
    with pytest.raises(UnknownEffectError):
        ctx.unload("ghost")


# ---------------------------------------------------------------------------
# 事件：广播 / 瀑布 / 订阅随 effect 回卷
# ---------------------------------------------------------------------------


def test_emit_broadcasts_in_subscription_order():
    ctx = Context()
    seen: list[int] = []
    ctx.on("ping", lambda **kw: seen.append(kw["n"]))
    ctx.on("ping", lambda **kw: seen.append(kw["n"] * 10))
    ctx.emit("ping", n=1)
    assert seen == [1, 10]


def test_waterfall_lets_handlers_rewrite_value():
    ctx = Context()
    ctx.on("vars", lambda value, **kw: {**value, "b": 2})
    ctx.on("vars", lambda value, **kw: None)  # 不改 = 保持
    result = ctx.waterfall("vars", {"a": 1})
    assert result == {"a": 1, "b": 2}


def test_subscription_inside_effect_is_rolled_back_on_unload():
    ctx = Context()
    seen: list[str] = []

    def install():
        ctx.on("tick", lambda **kw: seen.append("subscribed"))
        return None

    ctx.effect("sub", install)
    ctx.emit("tick")
    assert seen == ["subscribed"]
    ctx.unload("sub")
    ctx.emit("tick")
    assert seen == ["subscribed"]  # 卸载后不再收到
    assert ctx.event_names == []


def test_explicit_unsubscribe_via_disposer():
    ctx = Context()
    seen: list[str] = []
    off = ctx.on("tick", lambda **kw: seen.append("x"))
    off()
    ctx.emit("tick")
    assert seen == []
