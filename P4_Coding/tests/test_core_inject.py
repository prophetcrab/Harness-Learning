"""动态依赖（ctx.inject）：依赖齐了才跑一块；依赖走了自动拆。

对应 dsh 里 `ctx.inject(['service'], ctx => ...)` 的 57 处用法。与静态 inject
的分工：

    静态 inject  = 整个插件等依赖（缺了 pending）
    ctx.inject   = 插件主体照常跑，**这一块**等依赖；依赖走了这块回卷

与 optional 的区别：optional 是"来了不重装"，动态 inject 是"来了就装、走了就拆"。
"""

from __future__ import annotations

import pytest

from core import Context

# ---------------------------------------------------------------------------
# 基本：等依赖 → 跑 → 依赖走了 → 拆 → 回来 → 再跑
# ---------------------------------------------------------------------------


def test_callback_runs_when_dependency_appears():
    ctx = Context()
    ran: list[str] = []

    ctx.inject(["store"], lambda c: ran.append(c.require("store")))
    assert ran == []                       # 依赖没来：不跑
    assert [i.status for i in ctx.injections] == ["pending"]
    assert ctx.injections[0].missing == ("store",)

    ctx.provide("store", "一号店")
    assert ran == ["一号店"]
    assert [i.status for i in ctx.injections] == ["active"]
    assert ctx.injections[0].missing == ()


def test_dependency_leaving_rolls_back_the_block_and_returning_reruns_it():
    ctx = Context()
    log: list[str] = []

    def register(c):
        log.append("start")
        c.provide("panel", "面板")

        def cleanup():
            log.append("stop")

        return cleanup

    ctx.inject(["store"], register)
    ctx.provide("store", 1)
    assert log == ["start"] and ctx.get("panel") == "面板"

    ctx.retract("store")
    # 依赖走了：回调的登记全部回卷（"走了就拆"），订阅保留
    assert log == ["start", "stop"]
    assert ctx.get("panel") is None
    assert [i.status for i in ctx.injections] == ["pending"]

    ctx.provide("store", 2)                 # 依赖回来：回调重跑
    assert log == ["start", "stop", "start"]
    assert ctx.get("panel") == "面板"


def test_multiple_dependencies_are_all_required():
    ctx = Context()
    got: list[tuple[str, str]] = []

    ctx.inject(["a", "b"], lambda c: got.append((c.require("a"), c.require("b"))))
    ctx.provide("a", "A")
    assert got == []
    assert ctx.injections[0].missing == ("b",)
    ctx.provide("b", "B")
    assert got == [("A", "B")]

    ctx.retract("a")                        # 少一个就拆
    assert got == [("A", "B")]
    assert ctx.injections[0].status == "pending"


# ---------------------------------------------------------------------------
# 取消订阅
# ---------------------------------------------------------------------------


def test_handle_cancels_subscription_and_rolls_back():
    ctx = Context()
    log: list[str] = []

    def register(c):
        c.provide("panel", 1)
        return lambda: log.append("cleanup")

    cancel = ctx.inject(["store"], register)
    ctx.provide("store", 1)
    assert ctx.get("panel") == 1

    cancel()
    assert log == ["cleanup"]
    assert ctx.get("panel") is None
    assert ctx.injections == []

    cancel()                                # 幂等
    ctx.retract("store")
    ctx.provide("store", 2)
    assert ctx.get("panel") is None         # 已取消：不会因为依赖回来又跑


def test_cancel_before_dependency_arrives():
    ctx = Context()
    ran: list[str] = []
    cancel = ctx.inject(["store"], lambda c: ran.append("ran"))
    cancel()
    ctx.provide("store", 1)
    assert ran == []
    assert ctx.injections == []


def test_handle_is_bound_to_its_own_subscription():
    """同名依赖的两条订阅互不影响：取消一条，另一条照常。"""
    ctx = Context()
    log: list[str] = []
    first = ctx.inject(["store"], lambda c: log.append("first"))
    ctx.inject(["store"], lambda c: log.append("second"))

    ctx.provide("store", 1)
    assert log == ["first", "second"]
    first()
    assert len(ctx.injections) == 1
    assert [i.status for i in ctx.injections] == ["active"]
    ctx.retract("store")
    ctx.provide("store", 2)
    assert log == ["first", "second", "second"]  # 只有第二条重跑


# ---------------------------------------------------------------------------
# 装进插件 / effect 里：订阅随外层一起走
# ---------------------------------------------------------------------------


def test_subscription_inside_plugin_is_cancelled_with_the_plugin():
    ctx = Context()
    log: list[str] = []

    class Owner:
        name = "owner"

        def setup(self, c):
            c.provide("owner-svc", 1)
            c.inject(["store"], lambda cc: log.append("block ran"))

    ctx.load_plugin(Owner())
    ctx.provide("store", 1)
    assert log == ["block ran"]

    ctx.unload_plugin("owner")
    assert ctx.injections == []             # 订阅随插件取消
    ctx.retract("store")
    ctx.provide("store", 2)
    assert log == ["block ran"]             # 不会再跑


def test_subscription_inside_effect_is_rolled_back_with_the_effect():
    ctx = Context()
    log: list[str] = []

    def install():
        ctx.inject(["store"], lambda c: log.append("inner"))
        return lambda: log.append("outer cleanup")

    ctx.effect("sub", install)
    ctx.provide("store", 1)
    assert log == ["inner"]

    ctx.unload_effect("sub")
    assert ctx.injections == []
    assert log == ["inner", "outer cleanup"]
    ctx.retract("store")
    ctx.provide("store", 2)
    assert log == ["inner", "outer cleanup"]


def test_block_registrations_are_children_of_the_subscription():
    """回调里再开 effect：依赖消失时按"子先父后"回卷。"""
    ctx = Context()
    log: list[str] = []

    def install_child(c):
        c.effect("child", lambda: (lambda: log.append("child out")))
        return lambda: log.append("block out")

    ctx.inject(["store"], install_child)
    ctx.provide("store", 1)
    ctx.retract("store")
    assert log == ["child out", "block out"]
    assert ctx.effects == []


# ---------------------------------------------------------------------------
# 失败语义
# ---------------------------------------------------------------------------


def test_callback_error_is_reported_and_not_retried_in_place():
    ctx = Context()

    def boom(c):
        raise RuntimeError("回调炸了")

    ctx.inject(["store"], boom)             # 依赖未齐：先挂着
    assert ctx.injections[0].status == "pending"

    with pytest.raises(RuntimeError, match="回调炸了"):
        ctx.provide("store", 1)             # 依赖齐 → 跑回调 → 炸（抛回触发方）
    injection = ctx.injections[0]
    assert injection.status == "failed"
    assert isinstance(injection.error, RuntimeError)
    assert ctx.get("store") == 1            # 提供者本身没事

    # 依赖没变：不原地重试（再 provide 别的服务不会重新炸）
    ctx.provide("other", 1)
    assert ctx.get("other") == 1
    assert ctx.injections[0].status == "failed"

    # 依赖走了再回来：退回 pending，然后重试（回调还是必炸的那个）
    ctx.retract("store")
    assert ctx.injections[0].status == "pending"
    with pytest.raises(RuntimeError, match="回调炸了"):
        ctx.provide("store", 2)
    assert ctx.injections[0].status == "failed"


def test_callback_error_when_registered_against_existing_dependency():
    ctx = Context()
    ctx.provide("store", 1)

    def boom(c):
        raise ValueError("注册时就炸")

    with pytest.raises(ValueError, match="注册时就炸"):
        ctx.inject(["store"], boom)
    assert [i.status for i in ctx.injections] == ["failed"]
    assert ctx.effects == []                # 半装已回卷


def test_cleanup_error_during_stop_fails_loud_but_still_unwinds():
    ctx = Context()
    log: list[str] = []

    def register(c):
        c.provide("panel", 1)

        def cleanup():
            log.append("cleanup ran")
            raise RuntimeError("清理炸了")

        return cleanup

    ctx.inject(["store"], register)
    ctx.provide("store", 1)

    with pytest.raises(RuntimeError, match="清理炸了"):
        ctx.retract("store")
    # fail loud，但回卷完成：登记撤掉、订阅退回 pending
    assert log == ["cleanup ran"]
    assert ctx.get("panel") is None
    assert [i.status for i in ctx.injections] == ["pending"]


# ---------------------------------------------------------------------------
# 与被解锁的插件协作
# ---------------------------------------------------------------------------


def test_block_provided_service_activates_waiting_plugin():
    ctx = Context()

    class Consumer:
        name = "consumer"
        inject = ("panel",)

        def setup(self, c):
            c.require("panel")

    ctx.load_plugin(Consumer())
    assert ctx.pending_plugins == ["consumer"]

    ctx.inject(["store"], lambda c: c.provide("panel", "我"))
    ctx.provide("store", 1)
    assert ctx.active_plugins == ["consumer"]

    ctx.retract("store")                    # 块拆掉 → 服务消失 → 消费者退位
    assert ctx.pending_plugins == ["consumer"]


def test_two_blocks_can_depend_on_each_other_through_services():
    ctx = Context()
    log: list[str] = []

    ctx.inject(["base"], lambda c: c.provide("middle", "M"))
    ctx.inject(["middle"], lambda c: log.append("second block ran"))
    assert log == []

    ctx.provide("base", "B")
    assert log == ["second block ran"]

    ctx.retract("base")
    assert log == ["second block ran"]
    assert [i.status for i in ctx.injections] == ["pending", "pending"]


# ---------------------------------------------------------------------------
# 声明校验 / 观测
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "store",            # 单个字符串（最常见的笔误）
        [],                 # 空
        ("",),              # 空服务名
        ("a", "a"),         # 重复
    ],
)
def test_invalid_deps_fail_loud(bad):
    ctx = Context()
    with pytest.raises((TypeError, ValueError)):
        ctx.inject(bad, lambda c: None)
    assert ctx.injections == []


def test_callback_must_be_callable():
    ctx = Context()
    with pytest.raises(TypeError):
        ctx.inject(["store"], "不是函数")  # type: ignore[arg-type]


def test_injections_snapshot_is_readable():
    ctx = Context()
    ctx.inject(["a"], lambda c: None)
    ctx.inject(["b", "c"], lambda c: None)
    snapshot = ctx.injections
    assert [i.deps for i in snapshot] == [("a",), ("b", "c")]
    assert [i.status for i in snapshot] == ["pending", "pending"]
    assert snapshot[1].missing == ("b", "c")


def test_inject_effect_namespace_is_reserved():
    ctx = Context()
    with pytest.raises(ValueError):
        ctx.effect("inject:1", lambda: None)
