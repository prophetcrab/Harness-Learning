"""步 8 验收：启动器（core · 引导）+ 产品政策。

对应 PLAN.md「M1 拆步」第 8 步：空装配单能起；boot 产出可用的 Runtime；
产品政策（角色词表 + 至少一个应用化插件）只在起产品时执行。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from core import (
    BootError,
    Context,
    PolicyError,
    Runtime,
    boot,
    check_product_policy,
    dump,
)
from plugins.base.loader import AssemblyError


def make(name: str, *, provides: tuple[str, ...] = ()) -> Callable[..., object]:
    def factory(**_config: Any) -> object:
        class P:
            pass

        P.name = name

        def setup(self, ctx):
            for service in provides:
                ctx.provide(service, name)

        P.setup = setup
        return P()

    return factory


CATALOG = {
    "store": make("store", provides=("store",)),
    "greeter": make("greeter", provides=("greeting",)),
    "clock": make("clock", provides=("clock",)),
}


# ---------------------------------------------------------------------------
# 基座自足
# ---------------------------------------------------------------------------


def test_empty_manifest_boots():
    runtime = boot([], catalog=CATALOG)
    try:
        assert runtime.ctx.services == ["loader"]
        assert runtime.snapshot() == ()
        assert "空" in runtime.dump()
    finally:
        runtime.close()
    assert runtime.ctx.plugins == []


def test_boot_assembles_and_exposes_loader():
    runtime = boot(
        [
            {"id": "s", "name": "store", "role": "base"},
            {"id": "g", "name": "greeter", "role": "capability"},
        ],
        catalog=CATALOG,
    )
    try:
        assert runtime.ctx.require("store") == "store"
        assert [state.path for state in runtime.snapshot()] == ["s", "g"]
        assert runtime.loader is not None
        # 热插拔走同一个 loader 服务（没有第二个入口）
        runtime.loader.load({"id": "c", "name": "clock"})
        assert runtime.ctx.require("clock") == "clock"
    finally:
        runtime.close()


def test_boot_failure_does_not_return_a_half_world():
    with pytest.raises(BootError):
        boot([{"id": "x", "name": "不存在"}], catalog=CATALOG)

    def strict(**config: Any) -> object:
        raise TypeError(f"不认识的参数：{sorted(config)}")

    with pytest.raises(BootError):
        boot([{"id": "x", "name": "strict", "config": {"bad": 1}}],
             catalog={**CATALOG, "strict": strict})


def test_boot_wraps_assembly_error():
    with pytest.raises(BootError) as err:
        boot([{"id": "x", "name": "nope"}], catalog=CATALOG)
    assert isinstance(err.value.__cause__, AssemblyError)


def test_close_unloads_everything_including_disabled_entries():
    runtime = boot([{"id": "s", "name": "store", "role": "base"}], catalog=CATALOG)
    runtime.loader.disable("s")  # 停用：登记还在
    runtime.close()
    assert runtime.ctx.plugins == []
    assert runtime.ctx.services == []


def test_close_is_safe_after_manual_unload():
    runtime = boot([{"id": "s", "name": "store", "role": "base"}], catalog=CATALOG)
    runtime.loader.unload("s")
    runtime.close()  # 树空、loader 还在：照常关掉


# ---------------------------------------------------------------------------
# 产品政策
# ---------------------------------------------------------------------------


def test_product_without_application_fails():
    with pytest.raises(PolicyError, match="应用化插件"):
        boot(
            [{"id": "s", "name": "store", "role": "base"}],
            catalog=CATALOG,
            product=True,
        )


def test_empty_product_fails():
    with pytest.raises(PolicyError, match="应用化插件"):
        boot([], catalog=CATALOG, product=True)


def test_product_with_application_boots():
    runtime = boot(
        [
            {"id": "s", "name": "store", "role": "base"},
            {"id": "g", "name": "greeter", "role": "application"},
        ],
        catalog=CATALOG,
        product=True,
    )
    runtime.close()


def test_product_requires_role_on_every_plugin_entry():
    with pytest.raises(PolicyError, match="没声明"):
        boot(
            [
                {"id": "g", "name": "greeter", "role": "application"},
                {"id": "s", "name": "store"},  # 没声明 role
            ],
            catalog=CATALOG,
            product=True,
        )


def test_product_rejects_unknown_role():
    with pytest.raises(PolicyError, match="角色不合法"):
        boot(
            [{"id": "g", "name": "greeter", "role": "capabilty"}],  # 拼错
            catalog=CATALOG,
            product=True,
        )


def test_group_entries_need_no_role():
    runtime = boot(
        [
            {
                "id": "app",
                "children": [{"id": "g", "name": "greeter", "role": "application"}],
            }
        ],
        catalog=CATALOG,
        product=True,
    )
    runtime.close()


def test_disabled_application_does_not_satisfy_policy():
    """声明了 application 但关着 → 产品仍然"没有入口"。"""
    with pytest.raises(PolicyError, match="没在运行"):
        boot(
            [{"id": "g", "name": "greeter", "role": "application", "enabled": False}],
            catalog=CATALOG,
            product=True,
        )


def test_bare_assembly_skips_product_policy():
    """裸装配不检查产品政策（测试 / 嵌入式场景）。"""
    runtime = boot(
        [{"id": "s", "name": "store"}],  # 没 role、没有 application
        catalog=CATALOG,
        product=False,
    )
    runtime.close()


def test_policy_can_be_checked_standalone():
    runtime = boot([{"id": "g", "name": "greeter", "role": "application"}], catalog=CATALOG)
    try:
        check_product_policy(runtime)  # 不抛
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------


def test_dump_is_readable():
    runtime = boot(
        [
            {"id": "app", "children": [{"id": "g", "name": "greeter", "role": "application"}]},
            {"id": "s", "name": "store", "role": "base"},
        ],
        catalog=CATALOG,
    )
    try:
        text = dump(runtime)
        assert "条目" in text and "角色" in text and "状态" in text
        assert "app:g" in text and "application" in text
        assert "（分组）" in text
    finally:
        runtime.close()


def test_runtime_dump_matches_function():
    runtime = boot([], catalog=CATALOG)
    try:
        assert runtime.dump() == dump(runtime)
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# Runtime 的边界
# ---------------------------------------------------------------------------


def test_runtime_loader_requires_loader_service():
    ctx = Context()  # 没有 loader 的裸容器
    runtime = Runtime(ctx)
    with pytest.raises(KeyError):
        _ = runtime.loader
