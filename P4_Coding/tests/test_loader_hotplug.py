"""步 7 验收：热插拔（load / update / replace）+ 变更锁 + revision。

对应 PLAN.md「M1 拆步」第 7 步：

- `load` 运行中加一条；`update` 换 config；`replace` 换条目——全部**原子**：
  新的一份没装成功，旧的原样还在、服务不断档；
- 变更锁：并发变更不撕裂台账 / 树；
- revision：每次**有效**变更 +1，`last_change` 报告谁退位、谁激活。

"原子"在这里的准确含义（同名插件不能同时存在两份）：失败后旧配置重新装回来
——中间有一段服务缺席是必然的，但**结束时**世界与变更前等价。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from core import Context
from plugins.loader import LOADER_SERVICE, AssemblyError, LoaderService, loader_plugin


def make(
    name: str,
    *,
    provides: tuple[str, ...] = (),
    inject: tuple[str, ...] = (),
    label: str = "",
    fail_setup: bool = False,
    registry: list[str] | None = None,
) -> Callable[..., object]:
    """假插件工厂：config 里可带 label / fail_setup（后者用于"造新失败"）。"""

    def factory(*, label: str = "", fail_setup: bool = False, **_rest: Any) -> object:
        class P:
            pass

        P.name = name
        P.inject = tuple(inject)

        def setup(self, ctx):
            if fail_setup:
                raise RuntimeError(f"{name} 的 setup 按配置炸了")
            for dep in inject:
                ctx.require(dep)
            for service in provides:
                ctx.provide(service, label or name)
            if registry is not None:
                registry.append(f"{name}:{label or '-'}")

        P.setup = setup
        return P()

    # 工厂签名固定成 keyword-only，测试里从 config 传参
    def _factory(**config: Any) -> object:
        return factory(**config)

    return _factory


CATALOG: dict[str, Callable[..., object]] = {
    "store": make("store", provides=("store",)),
    "greeter": make("greeter", provides=("greeting",)),
    "waiter": make("waiter", provides=("waiter",), inject=("store",)),
    "flaky": make("flaky"),
}


def booted() -> tuple[Context, LoaderService]:
    ctx = Context()
    ctx.load_plugin(loader_plugin(CATALOG))
    return ctx, ctx.require(LOADER_SERVICE)


# ---------------------------------------------------------------------------
# load：运行中加一条
# ---------------------------------------------------------------------------


def test_load_adds_one_entry():
    ctx, loader = booted()
    path = loader.load({"id": "s", "name": "store", "config": {"label": "一号店"}})
    assert path == "s"
    assert ctx.require("store") == "一号店"
    assert [s.path for s in loader.snapshot()] == ["s"]


def test_load_under_parent_inherits_switch():
    ctx, loader = booted()
    loader.assemble([{"id": "app", "enabled": False, "children": []}])
    path = loader.load({"id": "s", "name": "store"}, parent="app")
    assert path == "app:s"
    assert ctx.get("store") is None  # 父关着：进树但不运行
    assert {s.path: s.status for s in loader.snapshot()} == {
        "app": "skipped",
        "app:s": "skipped",
    }
    loader.enable("app")
    assert ctx.require("store") == "store"


def test_load_path_collision_fails_loud():
    _ctx, loader = booted()
    loader.load({"id": "s", "name": "store"})
    with pytest.raises(AssemblyError, match="占用"):
        loader.load({"id": "s", "name": "greeter"})


def test_load_unknown_parent_fails_loud():
    _ctx, loader = booted()
    with pytest.raises(AssemblyError, match="父条目不存在"):
        loader.load({"id": "s", "name": "store"}, parent="nope")


def test_load_with_children_is_rejected():
    _ctx, loader = booted()
    with pytest.raises(AssemblyError, match="一次只加一条"):
        loader.load(
            {"id": "app", "children": [{"id": "s", "name": "store"}]}
        )


def test_failed_load_leaves_no_trace():
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="炸了"):
        loader.load({"id": "bad", "name": "flaky", "config": {"fail_setup": True}})
    assert loader.snapshot() == ()
    assert ctx.services == [LOADER_SERVICE]


def test_load_with_unmet_dependency_fails_loud():
    """单条热插拔也做残留检查：缺依赖当场报，不留给运行时。"""
    ctx, loader = booted()
    with pytest.raises(AssemblyError, match="等待依赖"):
        loader.load({"id": "w", "name": "waiter"})
    assert loader.snapshot() == ()
    assert ctx.services == [LOADER_SERVICE]


# ---------------------------------------------------------------------------
# update：换 config 重装（原子）
# ---------------------------------------------------------------------------


def test_update_replaces_config():
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store", "config": {"label": "旧店"}}])
    assert ctx.require("store") == "旧店"

    loader.update("s", {"label": "新店"})
    assert ctx.require("store") == "新店"
    assert loader.snapshot()[0].status == "active"


def test_update_failure_restores_old_config():
    """造新失败 → 旧配置重新装回来（世界与变更前等价），错误如实报出。"""
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store", "config": {"label": "旧店"}}])

    with pytest.raises(AssemblyError, match="已恢复旧 config"):
        loader.update("s", {"label": "新店", "fail_setup": True})

    assert ctx.require("store") == "旧店"  # 旧世界回来了
    assert loader.snapshot()[0].status == "active"


def test_update_of_disabled_entry_does_not_start_it():
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store", "config": {"label": "旧店"}}])
    loader.disable("s")
    loader.update("s", {"label": "新店"})
    assert ctx.get("store") is None  # 关着的条目不会被 update 顺手打开
    assert {s.path: s.status for s in loader.snapshot()} == {"s": "skipped"}

    loader.enable("s")
    assert ctx.require("store") == "新店"  # 新 config 在启用时生效


def test_update_unknown_entry_fails_loud():
    _ctx, loader = booted()
    with pytest.raises(AssemblyError, match="条目不存在"):
        loader.update("nope", {})


def test_update_keeps_dependents_alive_at_the_end():
    """换提供者：依赖者中途退位、结束时自动复活。"""
    ctx, loader = booted()
    loader.assemble(
        [
            {"id": "s", "name": "store", "config": {"label": "旧店"}},
            {"id": "w", "name": "waiter"},
        ]
    )
    loader.update("s", {"label": "新店"})
    assert ctx.require("store") == "新店"
    statuses = {s.path: s.status for s in loader.snapshot()}
    assert statuses == {"s": "active", "w": "active"}


# ---------------------------------------------------------------------------
# replace：换条目（同 id）
# ---------------------------------------------------------------------------


def test_replace_swaps_implementation_and_keeps_children():
    ctx, loader = booted()
    loader.assemble(
        [
            {
                "id": "app",
                "name": "store",
                "config": {"label": "应用"},
                "children": [{"id": "w", "name": "waiter"}],
            }
        ]
    )
    loader.replace("app", {"id": "app", "name": "greeter", "config": {"label": "应用"}})

    states = {s.path: s for s in loader.snapshot()}
    assert states["app"].name == "greeter"
    assert states["app"].status == "active"
    assert "app:w" in states and states["app:w"].status == "pending"  # store 没了，waiter 等依赖
    assert ctx.get("greeting") == "应用"


def test_replace_requires_same_id_and_no_children():
    _ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store"}])
    with pytest.raises(AssemblyError, match="id 必须是"):
        loader.replace("s", {"id": "other", "name": "store"})
    with pytest.raises(AssemblyError, match="不能带子树"):
        loader.replace("s", {"id": "s", "name": "greeter", "children": [{"id": "kid", "name": "store"}]})


def test_replace_failure_restores_old_entry():
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store", "config": {"label": "旧店"}}])

    with pytest.raises(AssemblyError, match="已恢复旧条目"):
        loader.replace("s", {"id": "s", "name": "flaky", "config": {"fail_setup": True}})

    assert ctx.require("store") == "旧店"
    assert loader.snapshot()[0].name == "store"


# ---------------------------------------------------------------------------
# revision / 变更报告
# ---------------------------------------------------------------------------


def test_revision_advances_on_effective_changes_only():
    ctx, loader = booted()
    start = ctx.revision

    loader.load({"id": "s", "name": "store"})
    after_load = ctx.revision
    assert after_load == start + 1  # 一次用户动作 = 一个 revision

    loader.load({"id": "g", "name": "greeter"})
    assert ctx.revision == after_load + 1

    # 失败的变更（世界回滚干净）不推进 revision
    before_fail = ctx.revision
    with pytest.raises(AssemblyError):
        loader.load({"id": "bad", "name": "flaky", "config": {"fail_setup": True}})
    assert ctx.revision == before_fail


def test_change_report_names_retired_and_activated():
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store"}])
    ctx.load_plugin(CATALOG["waiter"]())  # 外部依赖者（不在树里）

    loader.unload("s")
    report = ctx.last_change
    assert report is not None
    assert report.revision == ctx.revision
    assert "store" in report.retired
    assert "waiter" in report.retired  # 依赖者被连带退位


def test_nested_changes_collapse_into_one_revision():
    """插件 setup 里的 provide / load 并入最外层那一次变更。"""

    class Nested:
        name = "nested"

        def setup(self, ctx):
            ctx.provide("nested-svc", 1)
            ctx.load_plugin(CATALOG["store"]())

    ctx = Context()
    before = ctx.revision
    ctx.load_plugin(Nested())
    assert ctx.revision == before + 1  # 不是 3


def test_snapshot_reflects_revision():
    ctx, loader = booted()
    loader.load({"id": "s", "name": "store"})
    rev = ctx.revision
    loader.disable("s")
    assert ctx.revision > rev


# ---------------------------------------------------------------------------
# 并发：变更锁
# ---------------------------------------------------------------------------


def test_concurrent_loads_do_not_tear_the_tree():
    """6 个线程各装 5 条**互不相同**的插件（同插件名只能一份实例——这是 core 的纪律）。"""
    names = [f"svc{i}" for i in range(30)]
    catalog: dict[str, Callable[..., object]] = {
        name: make(name, provides=(name,)) for name in names
    }
    ctx = Context()
    ctx.load_plugin(loader_plugin(catalog))
    loader = ctx.require(LOADER_SERVICE)

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for n in range(5):
                name = names[index * 5 + n]
                loader.load({"id": f"w{index}-{n}", "name": name})
        except BaseException as exc:  # noqa: BLE001 —— 线程里的错误收集起来断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(loader.snapshot()) == 30
    assert len([s for s in loader.snapshot() if s.status == "active"]) == 30


def test_concurrent_toggle_and_snapshot_are_consistent():
    ctx, loader = booted()
    loader.assemble([{"id": "s", "name": "store"}])
    stop = threading.Event()
    seen: list[str] = []
    corrupted: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                # 快照永远要能读，且状态只能是已知值（不能读到半写状态）
                for state in loader.snapshot():
                    seen.append(state.status)
            except BaseException as exc:  # noqa: BLE001 —— 收集起来断言
                corrupted.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(20):
            loader.disable("s")
            loader.enable("s")
    finally:
        stop.set()
        thread.join()

    assert corrupted == []
    assert set(seen) <= {"active", "skipped", "disabled"}
    assert ctx.get("store") is not None  # 最后是启用状态


def test_reader_never_sees_a_half_loaded_entry():
    """装配是**整体可见**的：读者看不到"条目已进树、插件还没装"的中间态。

    这条是变更锁的确定性验收：让工厂慢慢造（sleep），读者在装载途中反复取快照。
    持锁时快照要么在装载前（没有这个条目）、要么在装载后（active）；不持锁时
    读者会大量看到 `unloaded`。
    """
    import time

    def slow_factory(**_: Any) -> object:
        time.sleep(0.004)  # 把"构造 + 装载"的窗口拉长到读者一定能撞上

        class Slow:
            name = "slow"

            def setup(self, ctx):
                ctx.provide("slow-svc", 1)

        return Slow()

    ctx = Context()
    ctx.load_plugin(loader_plugin({"slow": slow_factory}))
    loader = ctx.require(LOADER_SERVICE)

    stop = threading.Event()
    half_states: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            for state in loader.snapshot():
                if state.status == "unloaded":
                    half_states.append(state.path)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        path = loader.load({"id": "s", "name": "slow"})
    finally:
        stop.set()
        thread.join()

    assert path == "s"
    assert half_states == []  # 从未看到半装的条目
    assert ctx.get("slow-svc") == 1
