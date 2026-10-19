"""步 6 验收：树与所有权级联（卸父先卸子、深的先退；开关以子树为单位）。

对应 PLAN.md「M1 拆步」第 6 步。测试用的树：

    app(插件 app，提供 app-svc)
    ├── store(插件 store，提供 store-svc，依赖 app-svc)
    └── ui(插件 ui，提供 ui-svc，依赖 app-svc)
        └── clock(插件 clock，提供 clock-svc，依赖 ui-svc)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from core import Context
from plugins.base.loader import LOADER_SERVICE, AssemblyError, LoaderService, loader_plugin


def make(
    name: str,
    *,
    provides: tuple[str, ...] = (),
    inject: tuple[str, ...] = (),
    log: list[str] | None = None,
    boom: bool = False,
    on_cleanup: Callable[[Context], None] | None = None,
    registry: list[tuple[str, object]] | None = None,
) -> Callable[..., object]:
    """造一个插件工厂：声明提供 / 依赖的服务；清理可选记录、可选观察、可选炸。"""

    def factory(**_config: Any) -> object:
        class P:
            pass

        P.name = name
        P.inject = tuple(inject)

        def setup(self, ctx):
            for dep in inject:
                ctx.require(dep)
            for service in provides:
                ctx.provide(service, name)
            if log is not None:
                log.append(f"{name}:in")

            def cleanup():
                if log is not None:
                    log.append(f"{name}:out")
                if on_cleanup is not None:
                    on_cleanup(ctx)
                if boom:
                    raise RuntimeError(f"{name} 清理炸了")

            return cleanup

        P.setup = setup
        instance = P()
        if registry is not None:
            registry.append((name, instance))
        return instance

    return factory


def tree_catalog(
    log: list[str] | None = None,
    registry: list[tuple[str, object]] | None = None,
) -> dict[str, Callable[..., object]]:
    return {
        "app": make("app", provides=("app-svc",), log=log, registry=registry),
        "store": make(
            "store", provides=("store-svc",), inject=("app-svc",), log=log, registry=registry
        ),
        "ui": make("ui", provides=("ui-svc",), inject=("app-svc",), log=log, registry=registry),
        "clock": make(
            "clock", provides=("clock-svc",), inject=("ui-svc",), log=log, registry=registry
        ),
    }


TREE: list[dict[str, Any]] = [
    {
        "id": "app",
        "name": "app",
        "children": [
            {"id": "store", "name": "store"},
            {"id": "ui", "name": "ui", "children": [{"id": "clock", "name": "clock"}]},
        ],
    },
]


def booted(
    catalog: Mapping[str, Callable[..., object]],
) -> tuple[Context, LoaderService]:
    ctx = Context()
    ctx.load_plugin(loader_plugin(catalog))
    return ctx, ctx.require(LOADER_SERVICE)


def tree(
    log: list[str] | None = None,
    registry: list[tuple[str, object]] | None = None,
) -> tuple[Context, LoaderService]:
    ctx, loader = booted(tree_catalog(log, registry))
    loader.assemble(TREE)
    return ctx, loader


def status(ctx: Context, name: str) -> str:
    return next(info.status for info in ctx.plugins if info.name == name)


def statuses(loader: LoaderService) -> dict[str, str]:
    return {state.path: state.status for state in loader.snapshot()}


# ---------------------------------------------------------------------------
# 树的形状
# ---------------------------------------------------------------------------


def test_assemble_builds_tree_paths_and_parents():
    _ctx, loader = tree()
    states = loader.snapshot()
    assert [s.path for s in states] == ["app", "app:store", "app:ui", "app:ui:clock"]
    assert [s.parent for s in states] == [None, "app", "app", "app:ui"]


# ---------------------------------------------------------------------------
# 卸载子树
# ---------------------------------------------------------------------------


def test_unload_subtree_takes_everything_with_it():
    ctx, loader = tree()
    paths = loader.unload("app")
    assert paths == ("app", "app:store", "app:ui", "app:ui:clock")
    assert loader.snapshot() == ()  # 条目离开树
    assert ctx.services == [LOADER_SERVICE]  # 服务一个不剩（loader 自己还在）


def test_unload_leaf_keeps_the_rest():
    ctx, loader = tree()
    loader.unload("app:store")
    assert "store-svc" not in ctx.services
    assert "app-svc" in ctx.services and "ui-svc" in ctx.services
    assert [s.path for s in loader.snapshot()] == ["app", "app:ui", "app:ui:clock"]


def test_unload_retires_children_before_parent():
    """深的先退：子插件清理时，它依赖的（父的）服务还在；父清理时子已经走了。"""
    seen: dict[str, bool] = {}
    log: list[str] = []
    catalog = {
        "app": make(
            "app",
            provides=("app-svc",),
            log=log,
            on_cleanup=lambda ctx: seen.update(parent_sees_child_gone=ctx.get("panel-svc") is None),
        ),
        "panel": make(
            "panel",
            provides=("panel-svc",),
            inject=("app-svc",),
            log=log,
            on_cleanup=lambda ctx: seen.update(child_sees_parent_alive=ctx.get("app-svc") is not None),
        ),
    }
    ctx, loader = booted(catalog)
    loader.assemble([{"id": "app", "name": "app", "children": [{"id": "panel", "name": "panel"}]}])

    loader.unload("app")
    assert log == ["app:in", "panel:in", "panel:out", "app:out"]
    assert seen == {"child_sees_parent_alive": True, "parent_sees_child_gone": True}


def test_unload_unknown_or_twice_fails_loud():
    _ctx, loader = tree()
    with pytest.raises(AssemblyError, match="条目不存在"):
        loader.unload("nope")
    loader.unload("app:store")
    with pytest.raises(AssemblyError, match="条目不存在"):
        loader.unload("app:store")


def test_unload_subtree_after_disable():
    """停用过的子树照常能卸（登记还在，core 的 unload 对 disabled 记录一样有效）。"""
    ctx, loader = tree()
    loader.disable("app")
    loader.unload("app")
    assert loader.snapshot() == ()
    assert ctx.services == [LOADER_SERVICE]


def test_cleanup_failure_during_unload_still_tears_down():
    log: list[str] = []
    catalog = {
        "app": make("app", provides=("app-svc",), log=log),
        "bad": make("bad", boom=True, log=log),
        "good": make("good", log=log),
    }
    ctx, loader = booted(catalog)
    loader.assemble(
        [
            {
                "id": "app",
                "name": "app",
                "children": [
                    {"id": "bad", "name": "bad"},
                    {"id": "good", "name": "good"},
                ],
            }
        ]
    )
    with pytest.raises(AssemblyError, match="bad"):
        loader.unload("app")
    # 一个清理炸了不半途而废：树摘干净、服务撤干净
    assert loader.snapshot() == ()
    assert ctx.services == [LOADER_SERVICE]
    assert log == ["app:in", "bad:in", "good:in", "good:out", "bad:out", "app:out"]


# ---------------------------------------------------------------------------
# 开关子树（disable / enable）
# ---------------------------------------------------------------------------


def test_disable_subtree_stops_everything_and_enable_restores():
    ctx, loader = tree()
    affected = loader.disable("app")
    assert affected == ("app:ui:clock", "app:ui", "app:store", "app")  # 深的先退
    assert ctx.services == [LOADER_SERVICE]
    assert statuses(loader) == {
        "app": "skipped",
        "app:store": "skipped",
        "app:ui": "skipped",
        "app:ui:clock": "skipped",
    }
    # 登记还在：core 里是 disabled（一键可恢复）
    assert status(ctx, "store") == "disabled"

    affected = loader.enable("app")
    assert affected == ("app", "app:store", "app:ui", "app:ui:clock")  # 浅的先来
    for service in ("app-svc", "store-svc", "ui-svc", "clock-svc"):
        assert service in ctx.services
    assert all(s.status == "active" for s in loader.snapshot())


def test_disable_enable_reuses_the_same_instance():
    """停用只回卷，不丢对象；启用是同一个实例上 setup 重跑（core 的停用语义）。"""
    log: list[str] = []
    registry: list[tuple[str, object]] = []
    _ctx, loader = tree(log, registry)
    loader.disable("app:ui")
    loader.enable("app:ui")
    assert log.count("ui:in") == 2  # setup 跑了两遍
    assert len([1 for name, _ in registry if name == "ui"]) == 1  # 但只有一个实例


def test_disable_group_keeps_individual_switches():
    """开关继承：整层关掉再开回来，之前单独关掉的那条依然是关的。"""
    ctx, loader = tree()
    loader.disable("app:store")  # 单独关一件
    loader.disable("app")  # 整层关
    assert ctx.services == [LOADER_SERVICE]

    loader.enable("app")  # 整层开
    assert "app-svc" in ctx.services and "ui-svc" in ctx.services
    assert "store-svc" not in ctx.services  # 它自己的开关还关着
    assert statuses(loader)["app:store"] == "skipped"

    loader.enable("app:store")  # 单独开回来
    assert "store-svc" in ctx.services


def test_disable_reports_only_changed_entries():
    _ctx, loader = tree()
    loader.disable("app:store", )  # 先关一件
    affected = loader.disable("app")  # 整层关：已经关着的那件不在名单里
    assert affected == ("app:ui:clock", "app:ui", "app")


def test_enable_of_never_loaded_entry_loads_it():
    """装配时被声明关掉的条目：运行时开关打开 → 现装（不回写装配单）。"""
    ctx, loader = booted(tree_catalog())
    loader.assemble(
        [
            {
                "id": "app",
                "name": "app",
                "children": [{"id": "store", "name": "store", "enabled": False}],
            }
        ]
    )
    assert "store-svc" not in ctx.services
    assert statuses(loader)["app:store"] == "skipped"

    affected = loader.enable("app:store")
    assert affected == ("app:store",)
    assert ctx.require("store-svc") == "store"


def test_enable_under_disabled_ancestor_stays_off():
    """祖先还关着 → 子即使开关开回来也不运行（快照如实说 skipped）。"""
    ctx, loader = tree()
    loader.disable("app")
    loader.enable("app:store")
    assert "store-svc" not in ctx.services
    assert statuses(loader)["app:store"] == "skipped"

    loader.enable("app")  # 祖先开了，它才跑起来
    assert "store-svc" in ctx.services


def test_disable_group_without_plugin_is_fine():
    """纯分组（没有代码）也能开关：它只是把整层的开关拧掉。"""
    catalog = {"a": make("a", provides=("a-svc",)), "b": make("b", provides=("b-svc",))}
    ctx, loader = booted(catalog)
    loader.assemble(
        [
            {
                "id": "grp",
                "children": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
            }
        ]
    )
    assert loader.disable("grp") == ("grp:b", "grp:a")  # 分组自己不产出条目
    assert ctx.services == [LOADER_SERVICE]
    loader.enable("grp")
    assert "a-svc" in ctx.services and "b-svc" in ctx.services


def test_outsider_dependent_retires_and_returns():
    """两条级联一起生效：子树外依赖它的插件随开关退位、随开关回来。"""
    catalog = tree_catalog()
    catalog["outsider"] = make("outsider", provides=("out-svc",), inject=("ui-svc",))
    ctx, loader = booted(catalog)
    loader.assemble([*TREE, {"id": "outsider", "name": "outsider"}])

    loader.disable("app:ui")
    assert status(ctx, "outsider") == "pending"  # core 依赖级联：退位等依赖
    assert "out-svc" not in ctx.services

    loader.enable("app:ui")
    assert status(ctx, "outsider") == "active"


def test_bypass_unload_shows_up_as_unloaded_and_enable_reloads():
    """绕过 loader 直接卸插件：快照如实说 unloaded；enable 会把它装回来。"""
    ctx, loader = tree()
    ctx.unload_plugin("clock")
    assert statuses(loader)["app:ui:clock"] == "unloaded"
    loader.enable("app:ui:clock")
    assert ctx.require("clock-svc") == "clock"


# ---------------------------------------------------------------------------
# 装配失败 = 世界回到装配前（本步改的语义）
# ---------------------------------------------------------------------------


def test_failed_assemble_leaves_no_half_tree_and_retry_works():
    log: list[str] = []

    def make_boom():
        class Boom:
            name = "boom"

            def setup(self, ctx):
                ctx.provide("half", 1)
                raise RuntimeError("boom 的 setup 炸了")

        return Boom()

    catalog = tree_catalog(log)
    catalog["boom"] = make_boom
    ctx, loader = booted(catalog)

    with pytest.raises(AssemblyError):
        loader.assemble(
            [
                {"id": "app", "name": "app", "children": [{"id": "s", "name": "store"}]},
                {"id": "bad", "name": "boom"},
            ]
        )
    assert loader.snapshot() == ()  # 半截树也摘掉
    assert ctx.services == [LOADER_SERVICE]

    # 同一条单子（去掉坏条目）直接重试
    loader.assemble([{"id": "app", "name": "app", "children": [{"id": "s", "name": "store"}]}])
    assert ctx.require("store-svc") == "store"
