"""插件级依赖（requires）：声明"这些插件在场，本插件才完整"。

与 inject / optional 的分工（0045 补充）：

    inject    服务依赖  缺了 → pending（驱动生命周期）
    optional  服务依赖  缺了 → 照常激活（降级）
    requires  插件依赖  只做装配期校验（由 loader 执行），不驱动生命周期

为什么需要它：`llm` 没有 provider 时仍能提供路由服务，但任何调用都会失败。
这属于装配不完整，应该在起产品时 fail loud。而如果把 provider 做成 llm 的
**服务依赖**，provider 反过来又依赖 llm 服务——两边互相等，谁都不会激活。
"""

from __future__ import annotations

import pytest

from core import Context, matches_plugin_requirement
from plugins.base.loader import AssemblyError, LoaderService, loader_plugin
from plugins.capability.llm.fake import fake_llm_plugin


def factory(name: str, *, requires=()):
    def make(**_: object) -> object:
        class P:
            pass

        P.name = name
        P.requires = tuple(requires)

        def setup(self, ctx):
            ctx.provide(f"{name}-svc", name)

        P.setup = setup
        return P()

    return make


def booted(catalog) -> tuple[Context, LoaderService]:
    ctx = Context()
    ctx.load_plugin(loader_plugin(catalog))
    return ctx, ctx.require("loader")


# ---------------------------------------------------------------------------
# 名字匹配：带实例后缀
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required, plugin, expected",
    [
        ("llm-provider", "llm-provider", True),
        ("llm-provider", "llm-provider:deepseek", True),
        ("llm-provider", "llm-provider:openai", True),
        ("llm-provider", "llm-provider-x", False),
        ("llm", "llm-fake", False),
    ],
)
def test_requirement_name_matching(required, plugin, expected):
    assert matches_plugin_requirement(required, plugin) is expected


# ---------------------------------------------------------------------------
# 装配期校验
# ---------------------------------------------------------------------------


def test_requires_missing_fails_the_batch():
    _ctx, loader = booted({"core": factory("core", requires=("provider",))})
    with pytest.raises(AssemblyError, match="插件级依赖"):
        loader.assemble([{"id": "core", "name": "core"}])
    assert loader.snapshot() == ()


def test_requires_satisfied_by_suffixed_instance():
    ctx, loader = booted(
        {
            "core": factory("core", requires=("provider",)),
            "provider": factory("provider"),
        }
    )
    loader.assemble(
        [
            {"id": "core", "name": "core"},
            {"id": "deepseek", "name": "provider", "config": {"route": "deepseek"}},
        ]
    )
    assert ctx.require("core-svc") == "core"


def test_requires_satisfied_by_plugin_outside_the_manifest():
    """依赖可以是**已装载**的插件（装配单只补一部分是合法的）。"""
    ctx, loader = booted(
        {
            "core": factory("core", requires=("provider",)),
            "provider": factory("provider"),
        }
    )
    ctx.load_plugin(factory("provider")())  # 先绕过 loader 装一个
    loader.assemble([{"id": "core", "name": "core"}])
    assert ctx.require("core-svc") == "core"


def test_disabled_provider_does_not_satisfy_requires():
    """停用中的插件不算在场——一个关掉的 provider 满足不了"llm 可用"。"""
    ctx, loader = booted(
        {
            "core": factory("core", requires=("provider",)),
            "provider": factory("provider"),
        }
    )
    loader.assemble([{"id": "provider", "name": "provider"}])
    loader.disable("provider")
    with pytest.raises(AssemblyError, match="插件级依赖"):
        loader.assemble([{"id": "core", "name": "core"}])


def test_entry_explicitly_disabled_does_not_fail_the_batch():
    """装配方主动写 enabled=False：那是明确决定，不是漏装。"""
    _ctx, loader = booted({"core": factory("core", requires=("provider",))})
    loader.assemble([{"id": "core", "name": "core", "enabled": False}])
    assert [state.status for state in loader.snapshot()] == ["skipped"]


def test_hot_load_checks_requires_too():
    _ctx, loader = booted({"core": factory("core", requires=("provider",))})
    with pytest.raises(AssemblyError, match="插件级依赖"):
        loader.load({"id": "core", "name": "core"})
    assert loader.snapshot() == ()  # 失败的 load 不留痕迹


def test_replace_checks_requires_too():
    _ctx, loader = booted(
        {
            "a": factory("a"),
            "core": factory("core", requires=("provider",)),
        }
    )
    loader.assemble([{"id": "x", "name": "a"}])
    with pytest.raises(AssemblyError, match="插件级依赖"):
        loader.replace("x", {"id": "x", "name": "core"})
    assert loader.snapshot()[0].name == "a"  # 回滚到旧条目


# ---------------------------------------------------------------------------
# 台账可见性（不经 loader 也能看见不完整）
# ---------------------------------------------------------------------------


def test_plugin_info_reports_missing_requires():
    ctx = Context()
    ctx.load_plugin(factory("core", requires=("provider",))())
    info = next(item for item in ctx.plugins if item.name == "core")
    assert info.requires == ("provider",)
    assert info.missing_requires == ("provider",)

    ctx.load_plugin(factory("provider")())
    info = next(item for item in ctx.plugins if item.name == "core")
    assert info.missing_requires == ()


# ---------------------------------------------------------------------------
# LLM 的真实声明
# ---------------------------------------------------------------------------


def test_llm_requires_provider():
    from plugins.base.llm import llm_plugin

    ctx = Context()
    ctx.load_plugin(llm_plugin())
    info = next(item for item in ctx.plugins if item.name == "llm")
    assert info.requires == ("llm-provider",)


def test_llm_assembly_needs_a_real_provider():
    """假适配器不算：`llm-fake` 不满足 `llm-provider`（这是刻意的——假适配器
    是测试替身，不是"产品有了一条模型路由"）。"""
    from plugins.base.llm import llm_plugin

    _ctx, loader = booted({"llm": llm_plugin, "llm-fake": fake_llm_plugin})
    with pytest.raises(AssemblyError, match="llm-provider"):
        loader.assemble(
            [
                {"id": "llm", "name": "llm"},
                {"id": "fake", "name": "llm-fake"},
            ]
        )
