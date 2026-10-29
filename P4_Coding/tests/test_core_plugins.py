"""插件生命周期：装载 / 卸载 / 停用 / 启用 / 依赖声明 / 失败路径。"""

from __future__ import annotations

import pytest
from support import assert_steady, info, load, make_plugin

from core import (
    Context,
    DuplicatePluginError,
    MissingPluginError,
)


async def test_pending_until_dependency_appears(ctx: Context) -> None:
    user = make_plugin("user", inject=("s",))
    await ctx.load_plugin(user)
    assert info(ctx, "user").status == "pending"
    assert info(ctx, "user").missing == ("s",)
    assert_steady(ctx)

    await ctx.provide("s", 1)
    assert info(ctx, "user").status == "active"
    assert_steady(ctx)


async def test_load_order_does_not_matter(ctx: Context) -> None:
    provider = make_plugin("provider", provides=("s",))
    user = make_plugin("user", inject=("s",))
    await load(ctx, user, provider)  # 先装依赖者
    assert set(ctx.active_plugins) == {"provider", "user"}
    assert_steady(ctx)


async def test_unload_cascade_deep_first(ctx: Context) -> None:
    order: list[str] = []

    class Provider:
        name = "provider"

        async def setup(self, c: Context) -> object:
            await c.provide("p-out", 1)
            return lambda: order.append("provider")

    class Middle:
        name = "middle"
        inject = ("p-out",)

        async def setup(self, c: Context) -> object:
            await c.provide("m-out", 1)
            return lambda: order.append("middle")

    class Leaf:
        name = "leaf"
        inject = ("m-out",)

        def setup(self, c: Context) -> object:
            return lambda: order.append("leaf")

    await load(ctx, Provider(), Middle(), Leaf())
    await ctx.unload_plugin("provider")
    assert order == ["leaf", "middle", "provider"]
    assert [item.name for item in ctx.plugins] == ["middle", "leaf"]
    assert {item.status for item in ctx.plugins} == {"pending"}
    assert_steady(ctx)


async def test_unload_missing_plugin_raises(ctx: Context) -> None:
    with pytest.raises(MissingPluginError):
        await ctx.unload_plugin("nope")


async def test_duplicate_plugin_name_raises(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("p"))
    with pytest.raises(DuplicatePluginError):
        await ctx.load_plugin(make_plugin("p"))
    assert len(ctx.plugins) == 1


async def test_disable_keeps_registration_and_enable_restores_same_instance(
    ctx: Context,
) -> None:
    class P:
        name = "p"
        setups = 0

        async def setup(self, c: Context) -> None:
            P.setups += 1
            await c.provide("p-out", 1)

    plugin = P()
    await ctx.load_plugin(plugin)
    assert ctx.active_plugins == ["p"]

    affected = await ctx.disable_plugin("p")
    assert affected == ["p"]
    assert info(ctx, "p").status == "disabled"
    assert "p-out" not in ctx.services
    assert_steady(ctx)

    with pytest.raises(ValueError):
        await ctx.disable_plugin("p")

    await ctx.enable_plugin("p")
    assert info(ctx, "p").status == "active"
    assert P.setups == 2  # setup 在同一实例上重跑
    assert ctx.require("p-out") == 1


async def test_disable_cascades_and_enable_recovers_dependents(ctx: Context) -> None:
    provider = make_plugin("provider", provides=("s",))
    user = make_plugin("user", inject=("s",))
    await load(ctx, provider, user)

    await ctx.disable_plugin("provider")
    assert info(ctx, "provider").status == "disabled"
    assert info(ctx, "user").status == "pending"
    assert_steady(ctx)

    await ctx.enable_plugin("provider")
    assert set(ctx.active_plugins) == {"provider", "user"}
    assert_steady(ctx)


async def test_disable_and_enable_errors(ctx: Context) -> None:
    with pytest.raises(MissingPluginError):
        await ctx.disable_plugin("nope")
    with pytest.raises(MissingPluginError):
        await ctx.enable_plugin("nope")

    await ctx.load_plugin(make_plugin("active"))
    with pytest.raises(ValueError):
        await ctx.enable_plugin("active")

    await ctx.load_plugin(make_plugin("waiting", inject=("never",)))
    await ctx.disable_plugin("waiting")
    assert info(ctx, "waiting").status == "disabled"


async def test_pending_plugin_can_be_disabled_and_enabled(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("p", inject=("s",)))
    await ctx.disable_plugin("p")
    assert info(ctx, "p").status == "disabled"
    await ctx.enable_plugin("p")
    assert info(ctx, "p").status == "pending"


async def test_setup_failure_on_load_leaves_no_record(ctx: Context) -> None:
    bad = make_plugin("bad", setup_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await ctx.load_plugin(bad)
    assert ctx.plugins == []
    assert ctx.services == []
    assert ctx.effects == []
    assert ctx.revision == 0


async def test_setup_failure_triggered_by_other_operation_keeps_failed_record(
    ctx: Context,
) -> None:
    attempts: list[int] = []

    class Flaky:
        name = "flaky"
        inject = ("s",)

        async def setup(self, c: Context) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("first time boom")
            await c.provide("flaky-out", 1)

    await ctx.load_plugin(Flaky())
    with pytest.raises(RuntimeError, match="first time boom"):
        await ctx.provide("s", 1)

    record = info(ctx, "flaky")
    assert record.status == "failed"
    assert record.error is not None
    assert_steady(ctx)

    await ctx.enable_plugin("flaky")
    assert info(ctx, "flaky").status == "active"
    assert ctx.require("flaky-out") == 1


async def test_failure_from_another_plugin_leaves_new_plugin_loaded(ctx: Context) -> None:
    """装载可能"成功但报错"：另一个插件的激活失败从同一次 settle 抛出。

    这是 loader 已知并据此按名字回滚的场景，这里钉住当前语义。
    """

    class Target:
        name = "target"
        inject = ("s",)

        def setup(self, c: Context) -> None:
            raise RuntimeError("target boom")

    class Provider:
        name = "provider"

        async def setup(self, c: Context) -> None:
            await c.provide("s", 1)

    await ctx.load_plugin(Target())  # pending
    with pytest.raises(RuntimeError, match="target boom"):
        await ctx.load_plugin(Provider())

    assert info(ctx, "provider").status == "active"
    assert info(ctx, "target").status == "failed"
    assert_steady(ctx)


async def test_optional_dependency_degrade_and_no_restart(ctx: Context) -> None:
    log: list[str] = []
    plugin = make_plugin("p", optional=("opt",), log=log)
    await ctx.load_plugin(plugin)
    assert info(ctx, "p").status == "active"
    assert info(ctx, "p").missing_optionals == ("opt",)
    assert log == ["p:setup"]

    await ctx.provide("opt", 1)
    assert info(ctx, "p").status == "active"
    assert info(ctx, "p").missing_optionals == ()
    assert log == ["p:setup"]  # 可选依赖出现不重装


async def test_requires_is_assembly_time_only(ctx: Context) -> None:
    plugin = make_plugin("llm", requires=("llm-provider",))
    await ctx.load_plugin(plugin)
    assert info(ctx, "llm").status == "active"  # requires 不阻断生命周期
    assert info(ctx, "llm").missing_requires == ("llm-provider",)

    await ctx.load_plugin(make_plugin("llm-provider:deepseek"))
    assert info(ctx, "llm").missing_requires == ()


async def test_plugin_handle_is_idempotent_and_scoped(ctx: Context) -> None:
    handle = await ctx.load_plugin(make_plugin("p"))
    await ctx.unload_plugin("p")
    await handle()  # 已卸载：空操作
    assert ctx.plugins == []

    await ctx.load_plugin(make_plugin("p"))
    await handle()  # 同名新对象：旧句柄不认它
    assert "p" in ctx.active_plugins


async def test_reload_after_unload(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("p", provides=("s",)))
    await ctx.unload_plugin("p")
    await ctx.load_plugin(make_plugin("p", provides=("s",)))
    assert info(ctx, "p").status == "active"
    assert ctx.require("s") == "p"


async def test_unload_cleanup_failure_unloads_rest_and_raises_first(ctx: Context) -> None:
    order: list[str] = []

    class Bad:
        name = "bad"

        async def setup(self, c: Context) -> object:
            await c.provide("bad-out", 1)
            return lambda: (order.append("bad"), (_ for _ in ()).throw(RuntimeError("bad")))[0]

    class Dependent:
        name = "dependent"
        inject = ("bad-out",)

        def setup(self, c: Context) -> object:
            return lambda: order.append("dependent")

    await ctx.load_plugin(Bad())
    await ctx.load_plugin(Dependent())

    with pytest.raises(RuntimeError, match="bad"):
        await ctx.unload_plugin("bad")

    assert order == ["dependent", "bad"]
    assert [item.name for item in ctx.plugins] == ["dependent"]
    assert info(ctx, "dependent").status == "pending"
    assert_steady(ctx)


async def test_disable_cleanup_failure_still_disables_provider(ctx: Context) -> None:
    bad = make_plugin("bad", provides=("s",), cleanup_error=RuntimeError("clean boom"))
    user = make_plugin("user", inject=("s",))
    await load(ctx, bad, user)

    with pytest.raises(RuntimeError, match="clean boom"):
        await ctx.disable_plugin("bad")

    assert info(ctx, "bad").status == "disabled"
    assert info(ctx, "user").status == "pending"
    assert_steady(ctx)


async def test_missing_dependencies_and_snapshots(ctx: Context) -> None:
    await ctx.load_plugin(
        make_plugin("p", inject=("a", "b"), optional=("c",), provides=("out",))
    )
    assert ctx.missing_dependencies("p") == ["a", "b"]
    assert info(ctx, "p").provided == ()

    await ctx.provide("a", 1)
    await ctx.provide("b", 2)
    assert info(ctx, "p").provided == ("out",)
    assert ctx.missing_dependencies("p") == []
    with pytest.raises(MissingPluginError):
        ctx.missing_dependencies("nope")


async def test_failed_activation_restores_retracted_service_ownership(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("owner", provides=("k",)))

    class Bad:
        name = "bad"

        async def setup(self, c: Context) -> None:
            await c.retract("k")  # 越过契约去拆别人的槽（随后失败，应整表还原）
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await ctx.load_plugin(Bad())

    assert info(ctx, "owner").provided == ("k",)
    assert ctx.require("k") == "owner"
    assert_steady(ctx)


async def test_unload_non_active_plugins(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("pending", inject=("never",)))
    await ctx.disable_plugin("pending")
    await ctx.unload_plugin("pending")
    assert "pending" not in [item.name for item in ctx.plugins]
    assert_steady(ctx)

    with pytest.raises(RuntimeError, match="boom"):
        await ctx.load_plugin(make_plugin("failing", setup_error=RuntimeError("boom")))
    assert [item.name for item in ctx.plugins] == []


async def test_unload_failed_plugin_removes_record(ctx: Context) -> None:
    class Flaky:
        name = "flaky"
        inject = ("s",)

        def setup(self, c: Context) -> None:
            raise RuntimeError("boom")

    await ctx.load_plugin(Flaky())
    with pytest.raises(RuntimeError):
        await ctx.provide("s", 1)
    assert info(ctx, "flaky").status == "failed"

    await ctx.unload_plugin("flaky")
    assert ctx.plugins == []
    assert_steady(ctx)


async def test_load_handle_can_unload(ctx: Context) -> None:
    handle = await ctx.load_plugin(make_plugin("p", provides=("s",)))
    await handle()
    assert ctx.plugins == []
    assert ctx.services == []


async def test_disabled_plugins_property(ctx: Context) -> None:
    await ctx.load_plugin(make_plugin("a"))
    await ctx.load_plugin(make_plugin("b"))
    await ctx.disable_plugin("a")
    assert ctx.disabled_plugins == ["a"]
    assert ctx.active_plugins == ["b"]
