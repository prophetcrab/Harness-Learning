"""P3 内核的插件生命周期测试：inject 驱动的装载 / 停用 / 重激活。"""

from __future__ import annotations

import pytest

from kernel import (
    Context,
    DuplicatePluginError,
    UnknownPluginError,
    plugin_injects,
    plugin_name,
    plugin_optionals,
)


# 测试用插件：一个提供者 + 一个依赖者（记录自己的一生，供断言）。
class Provider:
    name = "provider"

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def setup(self, ctx: Context) -> None:
        self._log.append("provider.setup")
        ctx.provide("thing", "T")
        return lambda: self._log.append("provider.teardown")


class Consumer:
    name = "consumer"
    inject = ("thing",)

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def setup(self, ctx: Context) -> None:
        self._log.append(f"consumer.setup({ctx.require('thing')})")
        return lambda: self._log.append("consumer.teardown")


def test_plugin_activation_is_order_independent():
    """反序装载：consumer 先、provider 后——consumer 仍会拿到服务。"""
    log: list[str] = []
    ctx = Context()
    ctx.load_plugins([Consumer(log), Provider(log)])
    assert log == ["provider.setup", "consumer.setup(T)"]
    assert set(ctx.active_plugins) == {"consumer", "provider"}
    assert ctx.pending_plugins == []


def test_plugin_stays_pending_until_dependency_appears():
    log: list[str] = []
    ctx = Context()
    ctx.load_plugin(Consumer(log))
    assert ctx.pending_plugins == ["consumer"]
    assert ctx.missing_dependencies("consumer") == ["thing"]

    ctx.load_plugin(Provider(log))
    assert log == ["provider.setup", "consumer.setup(T)"]  # 依赖一就绪立刻激活
    assert set(ctx.active_plugins) == {"consumer", "provider"}
    assert ctx.pending_plugins == []


def test_unloading_provider_deactivates_consumer_then_reload_reactivates():
    log: list[str] = []
    ctx = Context()
    ctx.load_plugins([Provider(log), Consumer(log)])
    log.clear()

    ctx.unload_plugin("provider")
    assert log == ["consumer.teardown", "provider.teardown"]  # 消费者先退位
    assert ctx.pending_plugins == ["consumer"]

    ctx.load_plugin(Provider(log))
    assert log[-2:] == ["provider.setup", "consumer.setup(T)"]  # 自动重新激活
    assert set(ctx.active_plugins) == {"provider", "consumer"}
    assert ctx.pending_plugins == []


def test_batch_load_rolls_back_previously_loaded_on_failure():
    log: list[str] = []
    ctx = Context()

    class Broken:
        name = "broken"

        def setup(self, inner: Context) -> None:
            raise RuntimeError("第二个插件炸了")

    with pytest.raises(RuntimeError):
        ctx.load_plugins([Provider(log), Broken()])
    # 第一个插件被干净地拆掉，第二个失败记录不留在台账。
    assert log == ["provider.setup", "provider.teardown"]
    assert ctx.services == []
    assert ctx.plugins == []


def test_duplicate_and_unknown_plugin_fail_loud():
    log: list[str] = []
    ctx = Context()
    ctx.load_plugin(Provider(log))
    with pytest.raises(DuplicatePluginError):
        ctx.load_plugin(Provider(log))
    with pytest.raises(UnknownPluginError):
        ctx.unload_plugin("nobody")


def test_plain_function_is_a_plugin():
    ctx = Context()

    def greeting(ctx: Context) -> None:
        ctx.provide("hello", "world")

    ctx.load_plugin(greeting)
    assert ctx.require("hello") == "world"
    ctx.unload_plugin("greeting")  # 名字取函数名
    assert ctx.services == []


def test_plugin_declaration_helpers():
    class Named:
        name = "custom"
        inject = ("a", "b")

        def setup(self, ctx: Context) -> None: ...

    assert plugin_name(Named()) == "custom"
    assert plugin_injects(Named()) == ("a", "b")

    class Unnamed:
        def setup(self, ctx: Context) -> None: ...

    assert "Unnamed" in plugin_name(Unnamed())

    class BadInject:
        inject = "fs"  # 常见笔误：字符串会被逐字符迭代

        def setup(self, ctx: Context) -> None: ...

    with pytest.raises(TypeError):
        plugin_injects(BadInject())


def test_optional_dependency_does_not_block_activation():
    """`optional` 与 `inject` 的分工：可选依赖缺了照常激活、来了不重装、走了不停用。

    这是 `_05` 为界面插件引入的机制（见 kernel/plugin.py 的语义表）：
    "有则用、没有则降级，且不因它抖一下就被重启"。
    """
    log: list[str] = []

    class Ui:
        name = "ui"
        optional = ("thing",)

        def setup(self, ctx: Context):
            log.append("ui-setup")
            return lambda: log.append("ui-dispose")

    ctx = Context()
    ctx.load_plugin(Ui())
    assert ctx.active_plugins == ["ui"]          # 可选依赖缺失**不**阻塞激活
    info = ctx.plugins[0]
    assert info.inject == ()
    assert info.optional == ("thing",)
    assert info.missing_optionals == ("thing",)  # 但如实报告"缺哪些"

    ctx.provide("thing", object())               # 依赖来了
    assert log == ["ui-setup"], "可选依赖就绪**不该**重装插件"
    assert ctx.plugins[0].missing_optionals == ()

    ctx.retract("thing")                         # 依赖走了
    assert ctx.active_plugins == ["ui"], "可选依赖消失**不该**停用插件"
    assert log == ["ui-setup"]

    ctx.unload_plugin("ui")                      # 只有显式卸载才回卷
    assert log == ["ui-setup", "ui-dispose"]


def test_required_dependency_still_blocks_activation():
    """对照组：`inject` 的语义一点没变（缺了 pending、来了激活、走了停用）。"""
    log: list[str] = []

    class Worker:
        name = "worker"
        inject = ("thing",)

        def setup(self, ctx: Context):
            log.append("setup")
            return lambda: log.append("dispose")

    ctx = Context()
    ctx.load_plugin(Worker())
    assert ctx.pending_plugins == ["worker"]
    ctx.provide("thing", object())
    assert ctx.active_plugins == ["worker"] and log == ["setup"]
    ctx.retract("thing")
    assert ctx.active_plugins == [] and log == ["setup", "dispose"]


def test_optional_and_inject_cannot_overlap():
    """同一个服务要么必需要么可选——声明矛盾直接报错（fail loud）。"""

    class Confused:
        name = "confused"
        inject = ("thing",)
        optional = ("thing", "other")

        def setup(self, ctx: Context) -> None: ...

    with pytest.raises(ValueError, match="optional 与 inject 重复"):
        plugin_optionals(Confused())


def test_optional_must_be_sequence_not_string():
    class BadOptional:
        optional = "fs"  # 与 inject 同款笔误：字符串会被逐字符迭代

        def setup(self, ctx: Context) -> None: ...

    with pytest.raises(TypeError):
        plugin_optionals(BadOptional())


def test_plugin_info_snapshot_reports_status():
    log: list[str] = []
    ctx = Context()
    ctx.load_plugin(Consumer(log))
    info = ctx.plugins[0]
    assert info.name == "consumer"
    assert info.status == "pending"
    assert info.inject == ("thing",)
    assert info.missing == ("thing",)
