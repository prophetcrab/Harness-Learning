"""_07_Plugin_Effect 的单元测试：effect 台账、回卷语义、插件装载器。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（kernel/ 包：Context / effect / Plugin 装载器），以及它与 providers 侧的接线。

覆盖：
1. effect 基本：装载 → 登记 → 卸载回卷；provide 在装载期自动登记撤销
2. 顺序：LIFO（同级后进先出）；嵌套 effect 子先父后
3. fail loud：重复槽位 / 重复 effect / 未知 effect
4. 故障：装载失败回卷已装部分（不留半装）；disposer 抛错继续回卷其余
5. 插件协议：对象/函数两种形态；load_plugins 批量与故障回卷

运行：cd P2_Coding/_07_Plugin_Effect && python -m pytest -q
"""

from __future__ import annotations

import pytest

from kernel import (
    Context,
    DuplicateSlotError,
    Plugin,
    UnknownEffectError,
    load_plugin,
    load_plugins,
)

# =========================================================================
# 1) effect 基本：装载 → 登记 → 卸载回卷
# =========================================================================


def test_effect_registers_and_unwinds():
    ctx = Context()
    calls: list[str] = []

    def install():
        calls.append("装")
        ctx.provide("thing", 1)
        return lambda: calls.append("撤")

    dispose = ctx.effect("thing-plugin", install)
    assert ctx.slots == ["thing"] and ctx.effect_names == ["thing-plugin"]
    assert ctx.require("thing") == 1

    dispose()
    assert calls == ["装", "撤"]
    assert ctx.slots == [] and ctx.effect_names == []


def test_provide_during_load_is_auto_reversed():
    """注册即 effect：装载期 provide 的槽位，卸载时自动撤销（不用写 disposer）。"""
    ctx = Context()

    def install():
        ctx.provide("auto", "值")
        return None  # 刻意不返回 disposer

    ctx.effect("auto-plugin", install)
    assert ctx.slots == ["auto"]
    ctx.unload("auto-plugin")
    assert ctx.slots == []  # 仍然被回卷了


def test_provide_outside_effect_is_not_auto_reversed():
    """effect 外直接 provide 不自动登记（调用方自管，如测试脚手架）。"""
    ctx = Context()
    ctx.provide("manual", 1)
    assert ctx.get("manual") == 1


def test_get_with_default_and_require_loud():
    ctx = Context()
    assert ctx.get("missing") is None
    assert ctx.get("missing", "默认") == "默认"
    with pytest.raises(KeyError, match="未提供"):
        ctx.require("missing")


def test_retract_returns_value_and_is_loud():
    ctx = Context()
    ctx.provide("x", 42)
    assert ctx.retract("x") == 42
    assert ctx.slots == []
    with pytest.raises(KeyError, match="无法撤销"):
        ctx.retract("x")


# =========================================================================
# 2) 顺序：LIFO 与嵌套
# =========================================================================


def test_same_effect_auto_disposers_run_lifo():
    """同一 effect 里先后 provide 两样东西：撤销按后进先出（先撤后装的）。"""
    ctx = Context()
    order: list[str] = []

    def install():
        ctx.provide("a", 1)
        order.append("装 a")
        ctx.provide("b", 2)
        order.append("装 b")
        return None

    # provide 的自动撤销是幂等 pop——顺序不可直接观察，于是用一个"自报家门"的
    # disposer 作为第三个动作来验证 LIFO 的确是"后进先出"：
    def marker():
        order.append("撤 marker（第三个动作，最先执行）")

    ctx.effect("two", install)
    ctx._effects["two"].disposers.append(marker)  # noqa: SLF001 —— 单元测试观察顺序
    ctx.unload("two")
    assert order == ["装 a", "装 b", "撤 marker（第三个动作，最先执行）"]
    assert ctx.slots == []  # 自动撤销把两个槽位都清掉了


def test_nested_effects_unwind_children_first():
    """装载期注册的 effect 是子 effect：卸载父时子先撤、父后撤。"""
    ctx = Context()
    order: list[str] = []

    def outer_install():
        order.append("装 outer")
        ctx.register(lambda: (order.append("装 inner"), lambda: order.append("撤 inner"))[1])
        return lambda: order.append("撤 outer")

    ctx.effect("outer", outer_install)
    assert ctx.effect_names == ["outer", "auto:1"]  # 子登记在台账里
    ctx.unload("outer")
    assert order == ["装 outer", "装 inner", "撤 inner", "撤 outer"]
    assert ctx.effect_names == []  # 子树一并注销


def test_unloading_child_first_then_parent():
    """先单独卸载子 effect，父再卸载时不重复回卷它。"""
    ctx = Context()
    order: list[str] = []

    def outer_install():
        ctx.register(lambda: (None, lambda: order.append("撤 child"))[1])
        return lambda: order.append("撤 parent")

    ctx.effect("parent", outer_install)
    ctx.unload("auto:1")  # 先卸子
    ctx.unload("parent")
    assert order == ["撤 child", "撤 parent"]  # 不重复


# =========================================================================
# 3) fail loud
# =========================================================================


def test_duplicate_slot_is_loud():
    ctx = Context()
    ctx.provide("fs", object())
    with pytest.raises(DuplicateSlotError, match="槽位已存在"):
        ctx.provide("fs", object())


def test_duplicate_effect_is_loud():
    ctx = Context()
    ctx.effect("same", lambda: None)
    with pytest.raises(DuplicateSlotError, match="effect 已存在"):
        ctx.effect("same", lambda: None)


def test_unknown_effect_unload_is_loud():
    ctx = Context()
    with pytest.raises(UnknownEffectError, match="不存在"):
        ctx.unload("ghost")


# =========================================================================
# 4) 故障：不留半装；disposer 出错继续回卷
# =========================================================================


def test_failed_install_leaves_nothing_behind():
    """装载中途崩溃：已经 provide 的槽位也被回卷（半装状态比报错更糟）。"""
    ctx = Context()

    def broken():
        ctx.provide("half", "半装")
        raise RuntimeError("装载炸了")

    with pytest.raises(RuntimeError):
        ctx.effect("broken", broken)
    assert ctx.slots == []
    assert ctx.effect_names == []  # 台账里也不留


def test_disposer_error_still_unwinds_rest_and_raises():
    """一个 disposer 抛异常：继续回卷其余，最后把第一个异常抛出去。"""
    ctx = Context()
    order: list[str] = []

    def install():
        ctx.provide("a", 1)
        ctx.provide("b", 2)
        return None

    ctx.effect("p", install)

    def explode() -> None:
        raise ValueError("撤不动")

    ctx._effects["p"].disposers.append(explode)  # noqa: SLF001 —— 单元测试注入故障
    ctx._effects["p"].disposers.append(lambda: order.append("撤最后那个"))  # noqa: SLF001

    with pytest.raises(ValueError, match="撤不动"):
        ctx.unload("p")
    assert ctx.slots == []          # 其余 disposer 执行了
    assert order == ["撤最后那个"]


# =========================================================================
# 5) 插件协议与装载器
# =========================================================================


class GreetingPlugin:
    name = "greeting"

    def setup(self, ctx: Context):
        ctx.provide("greeting", "你好")
        return None


def test_plugin_object_load_and_unload():
    ctx = Context()
    dispose = load_plugin(ctx, GreetingPlugin())
    assert ctx.require("greeting") == "你好"
    assert ctx.effect_names == ["plugin:greeting"]
    dispose()
    assert ctx.slots == []


def test_plugin_function_form():
    """普通函数也是合法插件（函数名作为插件名）。"""
    ctx = Context()

    def echo_plugin(context: Context):
        context.provide("echo", "ping")

    load_plugin(ctx, echo_plugin)
    assert ctx.require("echo") == "ping"
    assert ctx.effect_names == ["plugin:echo_plugin"]


def test_plugin_protocol_is_satisfied():
    assert isinstance(GreetingPlugin(), Plugin)


def test_non_plugin_is_rejected():
    ctx = Context()
    with pytest.raises(TypeError, match="不是合法插件"):
        load_plugin(ctx, 42)


def test_load_plugins_batch_order_and_count():
    ctx = Context()

    def first(context: Context):
        context.provide("first", 1)

    def second(context: Context):
        context.provide("second", 2)

    disposers = load_plugins(ctx, [first, second])
    assert len(disposers) == 2
    assert ctx.slots == ["first", "second"]


def test_load_plugins_failure_rolls_back_loaded_part():
    """批量装载任一失败：已装成功的部分被回卷，异常继续抛。"""
    ctx = Context()

    def good(context: Context):
        context.provide("good", 1)

    def bad(context: Context):
        raise RuntimeError("第二个插件炸了")

    with pytest.raises(RuntimeError, match="第二个插件炸了"):
        load_plugins(ctx, [good, bad])
    assert ctx.slots == []   # good 装过的也被回卷
    assert ctx.effect_names == []


def test_plugin_reload_after_unload():
    """卸载后同一插件可重装（effect 名已注销）。"""
    ctx = Context()
    dispose = load_plugin(ctx, GreetingPlugin())
    dispose()
    dispose2 = load_plugin(ctx, GreetingPlugin())
    assert ctx.require("greeting") == "你好"
    dispose2()
