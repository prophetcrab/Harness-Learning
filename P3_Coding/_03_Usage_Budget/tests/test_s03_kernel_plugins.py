"""P3 内核的插件生命周期测试：inject 驱动的装载 / 停用 / 重激活。"""

from __future__ import annotations

import pytest

from kernel import (
    Context,
    DuplicatePluginError,
    UnknownPluginError,
    plugin_injects,
    plugin_name,
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


def test_plugin_info_snapshot_reports_status():
    log: list[str] = []
    ctx = Context()
    ctx.load_plugin(Consumer(log))
    info = ctx.plugins[0]
    assert info.name == "consumer"
    assert info.status == "pending"
    assert info.inject == ("thing",)
    assert info.missing == ("thing",)
