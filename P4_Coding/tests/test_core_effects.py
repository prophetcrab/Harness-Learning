"""步 3 验收：effect 回卷（注册即回卷）。

对应 PLAN.md「M1 拆步」第 3 步：装载期一切登记自动撤销；回卷"子先父后、
同级后进先出"；一个 disposer 抛错不打断其余；装载失败不留半装。

命名约定（测试里反复出现）：`*_in` = 装载时刻，`*_out` = 回卷时刻。
"""

from __future__ import annotations

import pytest

from core import Context, DuplicateEffectError, MissingEffectError

# ---------------------------------------------------------------------------
# 装载 / 卸载往返
# ---------------------------------------------------------------------------


def test_effect_roundtrip():
    ctx = Context()
    log: list[str] = []

    def install():
        log.append("in")
        return lambda: log.append("out")

    ctx.effect("job", install)
    assert log == ["in"]
    assert ctx.effects == ["job"]

    ctx.unload_effect("job")
    assert log == ["in", "out"]
    assert ctx.effects == []

    with pytest.raises(MissingEffectError):
        ctx.unload_effect("job")


def test_effect_without_own_disposer_is_fine():
    ctx = Context()
    ctx.effect("noop", lambda: None)
    ctx.unload_effect("noop")
    assert ctx.effects == []


def test_duplicate_name_and_reserved_prefix_fail_loud():
    ctx = Context()
    ctx.effect("x", lambda: None)
    with pytest.raises(DuplicateEffectError):
        ctx.effect("x", lambda: None)
    with pytest.raises(ValueError):
        ctx.effect("plugin:fake", lambda: None)  # 插件命名空间不许占用
    with pytest.raises(ValueError):
        ctx.effect("", lambda: None)
    assert ctx.effects == ["x"]


def test_install_returning_garbage_fails_loud_and_unwinds():
    ctx = Context()

    def install():
        ctx.provide("half", 1)
        return "不是撤销函数"

    with pytest.raises(TypeError):
        ctx.effect("bad", install)
    assert ctx.services == []   # 登记被回卷
    assert ctx.effects == []    # 名字不留在台账


# ---------------------------------------------------------------------------
# 注册即回卷：provide 自动挂撤销
# ---------------------------------------------------------------------------


def test_provide_inside_effect_is_auto_retracted():
    ctx = Context()
    ctx.effect("reg", lambda: ctx.provide("greeting", "你好"))
    assert ctx.require("greeting") == "你好"
    ctx.unload_effect("reg")
    assert "greeting" not in ctx.services
    assert ctx.services == []


def test_effect_loading_activates_waiting_plugin():
    """effect 里 provide 也能驱动依赖——装载完（出了装载期）就激活。"""

    class Consumer:
        name = "consumer"
        inject = ("store",)

        def setup(self, ctx):
            ctx.require("store")

    ctx = Context()
    ctx.load_plugin(Consumer())
    assert ctx.pending_plugins == ["consumer"]

    ctx.effect("provide-store", lambda: ctx.provide("store", {"items": []}))
    assert ctx.active_plugins == ["consumer"]

    ctx.unload_effect("provide-store")
    assert ctx.pending_plugins == ["consumer"]  # 服务随 effect 回卷 → 自动退位


# ---------------------------------------------------------------------------
# 嵌套：子先父后、同级后进先出
# ---------------------------------------------------------------------------


def test_nesting_unwinds_children_first_and_siblings_lifo():
    ctx = Context()
    log: list[str] = []

    def install_parent():
        def install_a():
            log.append("a:in")
            return lambda: log.append("a:out")

        def install_b():
            log.append("b:in")
            return lambda: log.append("b:out")

        ctx.effect("a", install_a)
        ctx.effect("b", install_b)
        log.append("parent:in")
        return lambda: log.append("parent:out")

    ctx.effect("parent", install_parent)
    assert log == ["a:in", "b:in", "parent:in"]

    ctx.unload_effect("parent")
    # 子先父后（同级后进先出）：b → a → parent
    assert log == ["a:in", "b:in", "parent:in", "b:out", "a:out", "parent:out"]
    assert ctx.effects == []


def test_child_can_be_unloaded_alone():
    ctx = Context()
    log: list[str] = []

    def install_parent():
        ctx.effect("child", lambda: (lambda: log.append("child:out")))
        return lambda: log.append("parent:out")

    ctx.effect("parent", install_parent)
    ctx.unload_effect("child")            # 单独卸子：只有它回卷
    assert log == ["child:out"]
    ctx.unload_effect("parent")           # 父之后卸载：不会重复回卷子
    assert log == ["child:out", "parent:out"]


# ---------------------------------------------------------------------------
# 清理失败不打断其余
# ---------------------------------------------------------------------------


def test_cleanup_error_does_not_stop_the_rest():
    ctx = Context()
    log: list[str] = []

    def boom():
        raise RuntimeError("cleanup 炸了")

    def install():
        ctx.effect("bad", lambda: boom)
        ctx.effect("good", lambda: (lambda: log.append("good")))
        return lambda: log.append("outer")

    ctx.effect("outer", install)
    with pytest.raises(RuntimeError, match="cleanup 炸了"):
        ctx.unload_effect("outer")
    # 三件事全部执行了（一个也没被跳过），最后重抛第一个异常
    assert log == ["good", "outer"]
    assert ctx.effects == []


# ---------------------------------------------------------------------------
# 装载失败：不留半装
# ---------------------------------------------------------------------------


def test_failed_install_unwinds_everything():
    ctx = Context()

    def install():
        ctx.provide("half", 1)
        ctx.effect("nested", lambda: ctx.provide("deep", 2))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        ctx.effect("bad", install)
    assert ctx.services == []   # 自己的、子 effect 的登记都撤了
    assert ctx.effects == []


def test_failed_nested_install_keeps_parent_alive():
    ctx = Context()
    log: list[str] = []

    def install_parent():
        ctx.provide("p-svc", 1)

        def bad():
            raise ValueError("子 effect 炸")

        with pytest.raises(ValueError):
            ctx.effect("bad-child", bad)  # 子失败：子自己回卷
        ctx.effect("ok-child", lambda: (lambda: log.append("ok:out")))
        return lambda: log.append("p:out")

    ctx.effect("parent", install_parent)
    assert ctx.require("p-svc") == 1       # 父没事
    assert ctx.effects == ["parent", "ok-child"]

    ctx.unload_effect("parent")
    assert log == ["ok:out", "p:out"]


def test_failed_plugin_setup_disposes_nested_effects_and_restores_slots():
    ctx = Context()
    ctx.provide("keep", "原值")
    log: list[str] = []

    class Bad:
        name = "bad"

        def setup(self, ctx):
            ctx.provide("half", 1)
            ctx.effect("bad-sub", lambda: (lambda: log.append("sub:out")))
            ctx.retract("keep")  # 拆了别人的台……
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        ctx.load_plugin(Bad())

    assert log == ["sub:out"]            # 子 effect 被回卷
    assert ctx.services == ["keep"]      # 整表还原：连被 retract 的也还回来
    assert ctx.require("keep") == "原值"
    assert ctx.plugins == []             # 失败记录不留台账
    assert ctx.effects == []


# ---------------------------------------------------------------------------
# 插件 = 一个 effect
# ---------------------------------------------------------------------------


def test_plugin_unload_takes_everything_with_it():
    """"卸根节点一个不剩"：服务、子 effect、自己的资源全跟着走。"""
    ctx = Context()
    log: list[str] = []

    class P:
        name = "p"

        def setup(self, ctx):
            ctx.provide("p-svc", 1)
            ctx.effect("p-sub", lambda: (lambda: log.append("sub:out")))
            return lambda: log.append("own:out")

    ctx.load_plugin(P())
    assert ctx.effects == ["plugin:p", "p-sub"]
    assert ctx.require("p-svc") == 1

    ctx.unload_plugin("p")
    # 子 effect 先（子先父后）；自己的撤销动作随后（同一 effect 内后进先出）
    assert log == ["sub:out", "own:out"]
    assert "p-svc" not in ctx.services
    assert ctx.effects == []
    assert ctx.services == []


def test_plugin_disposer_runs_before_its_services_are_retracted():
    """清理代码运行期间，它自己提供的服务还在（否则清理没法收尾）。"""
    ctx = Context()
    seen: dict[str, bool] = {}

    class P:
        name = "p"

        def setup(self, ctx):
            ctx.provide("p-svc", 1)

            def cleanup():
                seen["alive"] = ctx.get("p-svc") is not None

            return cleanup

    ctx.load_plugin(P())
    ctx.unload_plugin("p")
    assert seen["alive"] is True
    assert "p-svc" not in ctx.services


def test_plugin_effects_are_not_directly_unloadable():
    class P:
        name = "p"

        def setup(self, ctx):
            ctx.provide("p-svc", 1)

    ctx = Context()
    ctx.load_plugin(P())
    with pytest.raises(ValueError):
        ctx.unload_effect("plugin:p")
    assert ctx.require("p-svc") == 1  # 世界没被动过


def test_disable_then_enable_rebuilds_the_effect():
    class P:
        name = "p"

        def __init__(self):
            self.setups = 0

        def setup(self, ctx):
            self.setups += 1
            ctx.provide("p-svc", self.setups)

    plugin = P()
    ctx = Context()
    ctx.load_plugin(plugin)
    assert ctx.require("p-svc") == 1

    ctx.disable_plugin("p")
    assert "p-svc" not in ctx.services
    assert ctx.effects == []

    ctx.enable_plugin("p")           # 同一个对象、setup 重跑
    assert plugin.setups == 2
    assert ctx.require("p-svc") == 2


def test_cleanup_failure_during_unload_fails_loud():
    class P:
        name = "p"

        def setup(self, ctx):
            ctx.provide("p-svc", 1)

            def cleanup():
                raise RuntimeError("卸载时清理失败")

            return cleanup

    ctx = Context()
    ctx.load_plugin(P())
    with pytest.raises(RuntimeError, match="卸载时清理失败"):
        ctx.unload_plugin("p")
    # fail loud，但回卷照常完成：服务还是撤掉了（不留垃圾）
    assert "p-svc" not in ctx.services
    assert ctx.effects == []


# ---------------------------------------------------------------------------
# 句柄语义：认"这一次装载"，重复调用安全
# ---------------------------------------------------------------------------


def test_install_returning_child_handle_is_harmless():
    """`ctx.effect("p", lambda: ctx.effect("c", ...))`：返回子句柄不报错、不重复回卷。

    子 effect 已经随父回卷；父账本里那份子句柄再执行时，认的装载已经不在了
    ——必须是安全空操作，而不是 MissingEffectError。
    """
    ctx = Context()
    log: list[str] = []

    def child():
        log.append("child:in")
        return lambda: log.append("child:out")

    ctx.effect("parent", lambda: ctx.effect("child", child))
    assert log == ["child:in"]

    ctx.unload_effect("parent")
    assert log == ["child:in", "child:out"]  # 子只回卷一次
    assert ctx.effects == []


def test_effect_handle_is_idempotent():
    ctx = Context()
    off = ctx.effect("job", lambda: None)
    off()
    off()  # 再调一次：安全空操作（不是 MissingEffectError）
    assert ctx.effects == []


def test_effect_handle_is_bound_to_its_own_load():
    """句柄认"这一次装载"：同名新 effect 顶上后，旧句柄不会误伤它。"""
    ctx = Context()
    first = ctx.effect("x", lambda: ctx.provide("x-svc", "第一代"))
    ctx.unload_effect("x")
    ctx.effect("x", lambda: ctx.provide("x-svc", "第二代"))

    first()  # 空操作：它不是现在这次装载的
    assert ctx.require("x-svc") == "第二代"
    assert ctx.effects == ["x"]


def test_install_returning_subscription_disposer_is_not_registered_twice():
    """`lambda: ctx.on(...)`：on 已自动登记退订，返回同一份不重复记账。"""
    ctx = Context()
    ctx.effect("listen", lambda: ctx.on("evt", lambda: None))
    record = ctx._effects["listen"]  # 白盒：这条不变式外部没有可观测差异
    assert len(record.disposers) == 1
