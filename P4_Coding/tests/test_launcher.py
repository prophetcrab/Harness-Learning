"""引导：产品政策、dump、Runtime.close，以及用假 loader 跑通 boot 全流程。"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import ClassVar

import pytest
from support import make_plugin

import core
from core import BootError, Context, PolicyError, Runtime, boot, check_product_policy, dump


@dataclass(frozen=True)
class State:
    """loader 快照条目的最小形状（launcher 只读这些字段）。"""

    path: str
    name: str | None
    role: str | None
    status: str
    plugin: str | None = None
    missing: tuple[str, ...] = ()
    error: BaseException | None = None


class StubRuntime:
    def __init__(self, states: tuple[State, ...]) -> None:
        self._states = states

    def snapshot(self) -> tuple[State, ...]:
        return self._states


# ---------------------------------------------------------------------------
# 产品政策（同步读）
# ---------------------------------------------------------------------------


def test_policy_rejects_unroled_entry() -> None:
    runtime = StubRuntime([State("p", "p", None, "active")])
    with pytest.raises(PolicyError, match="role"):
        check_product_policy(runtime)


def test_policy_rejects_unknown_role() -> None:
    runtime = StubRuntime([State("p", "p", "service", "active")])
    with pytest.raises(PolicyError, match="角色不合法"):
        check_product_policy(runtime)


def test_policy_requires_running_application() -> None:
    runtime = StubRuntime(
        [
            State("base", "b", "base", "active"),
            State("app", "a", "application", "pending", missing=("s",)),
        ]
    )
    with pytest.raises(PolicyError, match="应用化插件"):
        check_product_policy(runtime)


def test_policy_passes_with_group_and_running_application() -> None:
    runtime = StubRuntime(
        [
            State("group", None, None, "group"),
            State("base", "b", "base", "active"),
            State("app", "a", "application", "active"),
        ]
    )
    check_product_policy(runtime)  # 不抛即通过


# ---------------------------------------------------------------------------
# dump（同步）
# ---------------------------------------------------------------------------


def test_dump_empty_message() -> None:
    assert "装配单是空的" in dump(StubRuntime(()))


def test_dump_renders_missing_and_error() -> None:
    runtime = StubRuntime(
        [
            State("ok", "ok-plugin", "base", "active", plugin="ok-plugin"),
            State("waiting", "w", "capability", "pending", missing=("a", "b")),
            State("broken", "x", "base", "failed", error=RuntimeError("boom")),
            State("group", None, None, "group"),
        ]
    )
    text = dump(runtime)
    assert "ok-plugin" in text
    assert "缺依赖：a, b" in text
    assert "RuntimeError: boom" in text
    assert "（分组）" in text


# ---------------------------------------------------------------------------
# Runtime.close（异步）
# ---------------------------------------------------------------------------


async def test_runtime_close_unloads_all_and_is_idempotent() -> None:
    ctx = Context()
    await ctx.load_plugin(make_plugin("a", provides=("a-out",)))
    await ctx.load_plugin(make_plugin("b", inject=("a-out",)))
    runtime = Runtime(ctx)

    await runtime.close()
    assert ctx.plugins == []
    assert ctx.services == []
    assert ctx.effects == []
    await runtime.close()  # 再来一次是空操作


async def test_runtime_close_continues_after_cleanup_error() -> None:
    ctx = Context()
    await ctx.load_plugin(make_plugin("bad", cleanup_error=RuntimeError("clean boom")))
    await ctx.load_plugin(make_plugin("other"))
    runtime = Runtime(ctx)

    with pytest.raises(RuntimeError, match="clean boom"):
        await runtime.close()

    assert ctx.plugins == []


# ---------------------------------------------------------------------------
# boot：用假 loader 模块跑通组合根
# ---------------------------------------------------------------------------


class FakeLoaderService:
    def __init__(self, ctx: Context, catalog: dict[str, object]) -> None:
        self.ctx = ctx
        self.catalog = catalog
        self._states: tuple[State, ...] = ()

    async def assemble(self, manifest: list[dict[str, object]]) -> None:
        states: list[State] = []
        for item in manifest:
            path = str(item["id"])
            name = item.get("name")
            role = item.get("role")
            if name is None:
                states.append(State(path, None, role, "group"))
                continue
            factory = self.catalog[str(name)]
            plugin = factory(**item.get("config", {}))  # type: ignore[operator]
            await self.ctx.load_plugin(plugin)
            plugin_name = core.plugin_name(plugin)
            snapshot = next(item for item in self.ctx.plugins if item.name == plugin_name)
            states.append(
                State(
                    path=path,
                    name=str(name),
                    role=role,
                    status=snapshot.status,
                    plugin=plugin_name,
                )
            )
        self._states = tuple(states)

    def snapshot(self) -> tuple[State, ...]:
        return self._states


class FakeLoaderPlugin:
    instances: ClassVar[list[FakeLoaderService]] = []

    def __init__(self, catalog: dict[str, object]) -> None:
        self.catalog = catalog

    async def setup(self, ctx: Context) -> None:
        service = FakeLoaderService(ctx, self.catalog)
        await ctx.provide("loader", service)
        FakeLoaderPlugin.instances.append(service)


@pytest.fixture()
def fake_loader(monkeypatch: pytest.MonkeyPatch) -> type[FakeLoaderPlugin]:
    FakeLoaderPlugin.instances.clear()

    plugins = types.ModuleType("plugins")
    plugins.__path__ = []  # type: ignore[attr-defined]
    base = types.ModuleType("plugins.base")
    base.__path__ = []  # type: ignore[attr-defined]
    loader = types.ModuleType("plugins.base.loader")
    loader.LOADER_SERVICE = "loader"  # type: ignore[attr-defined]
    loader.loader_plugin = FakeLoaderPlugin  # type: ignore[attr-defined]

    for name, module in {
        "plugins": plugins,
        "plugins.base": base,
        "plugins.base.loader": loader,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return FakeLoaderPlugin


def _catalog() -> dict[str, object]:
    return {
        "worker": lambda **_: make_plugin("worker", provides=("w-out",)),
        "app": lambda **_: make_plugin("app", provides=("app-out",)),
    }


async def test_boot_assembles_and_exposes_loader(fake_loader: type[FakeLoaderPlugin]) -> None:
    manifest = [
        {"id": "worker", "name": "worker", "role": "base"},
        {"id": "app", "name": "app", "role": "application"},
    ]
    runtime = await boot(manifest, catalog=_catalog())

    assert {"worker", "app"} <= set(runtime.ctx.active_plugins)
    assert runtime.loader is fake_loader.instances[-1]
    assert runtime.snapshot()[0].path == "worker"
    assert "worker" in runtime.dump()
    await runtime.close()
    assert runtime.ctx.plugins == []


async def test_boot_product_policy_failure_cleans_up(
    fake_loader: type[FakeLoaderPlugin],
) -> None:
    manifest = [{"id": "worker", "name": "worker", "role": "base"}]
    with pytest.raises(PolicyError):
        await boot(manifest, catalog=_catalog(), product=True)

    ctx = fake_loader.instances[-1].ctx
    assert ctx.plugins == []
    assert ctx.services == []


async def test_boot_assembly_failure_rolls_back_and_raises_boot_error(
    fake_loader: type[FakeLoaderPlugin],
) -> None:
    def explode(**_):
        raise RuntimeError("factory boom")

    with pytest.raises(BootError, match="启动失败"):
        await boot([{"id": "x", "name": "boom", "role": "base"}], catalog={"boom": explode})

    ctx = fake_loader.instances[-1].ctx
    assert ctx.plugins == []
    assert ctx.services == []


async def test_boot_product_success(fake_loader: type[FakeLoaderPlugin]) -> None:
    manifest = [
        {"id": "app", "name": "app", "role": "application"},
        {"id": "worker", "name": "worker", "role": "base"},
        {"id": "group", "role": None, "children": []},
    ]
    runtime = await boot(manifest, catalog=_catalog(), product=True)
    assert {"app", "worker"} <= set(runtime.ctx.active_plugins)
    await runtime.close()


async def test_runtime_close_unloads_pending_and_disabled() -> None:
    ctx = Context()
    await ctx.load_plugin(make_plugin("pending", inject=("never",)))
    await ctx.load_plugin(make_plugin("disabled"))
    await ctx.disable_plugin("disabled")

    await Runtime(ctx).close()
    assert ctx.plugins == []
    assert ctx.services == []


async def test_boot_empty_manifest(fake_loader: type[FakeLoaderPlugin]) -> None:
    runtime = await boot([], catalog={})

    loader_name = core.plugin_name(fake_loader({}))
    assert [item.name for item in runtime.ctx.plugins] == [loader_name]
    assert runtime.ctx.services == ["loader"]
    assert runtime.snapshot() == ()
    assert "装配单是空的" in runtime.dump()

    await runtime.close()
    assert runtime.ctx.plugins == []
    assert runtime.ctx.services == []


async def test_close_skips_plugin_already_unloaded_by_cascade() -> None:
    """close 的名字快照里可能有被别的插件清理时卸载掉的条目：跳过而不是报错。"""

    ctx = Context()
    await ctx.load_plugin(make_plugin("victim", provides=("v-out",)))

    class Killer:
        name = "killer"

        def setup(self, c: Context) -> object:
            return lambda: c.unload_plugin("victim")

    await ctx.load_plugin(Killer())
    await Runtime(ctx).close()
    assert ctx.plugins == []


async def test_boot_wraps_unexpected_error_but_passes_boot_error_through(
    fake_loader: type[FakeLoaderPlugin],
) -> None:
    def loader_plugin(catalog: object) -> object:
        raise BootError("loader factory boom")

    import sys as _sys

    _sys.modules["plugins.base.loader"].loader_plugin = loader_plugin  # type: ignore[attr-defined]
    with pytest.raises(BootError, match="loader factory boom"):
        await boot([], catalog={})
