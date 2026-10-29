"""随机操作序列下的不变量（固定种子，可复现）。

每步之后检查：台账无重名、active 的依赖齐备 / effect 存在 / provided 在服务表、
pending 必有缺失依赖、active 注入的依赖齐备、revision 单调。
"""

from __future__ import annotations

import random

import pytest
from support import assert_steady, make_plugin

from core import (
    Context,
    DuplicatePluginError,
    DuplicateSlotError,
    MissingPluginError,
    MissingServiceError,
)

SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "root-user": {"inject": ("root",), "provides": ("a",)},
    "a-user": {"inject": ("a",), "provides": ("b",)},
    "b-user": {"inject": ("b",)},
    "aux-user": {"inject": ("aux",), "provides": ("c",)},
    "c-user": {"inject": ("c",)},
    "solo": {"provides": ("d",)},
    "multi": {"inject": ("a", "d"), "provides": ("e",)},
}
EXTERNAL = ("root", "aux")


def _make(name: str) -> object:
    return make_plugin(name, **SPECS[name])


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11])
async def test_random_plugin_and_service_operations(seed: int) -> None:
    rng = random.Random(seed)
    ctx = Context()
    last_revision = 0

    for _ in range(250):
        op = rng.choice(["load", "unload", "disable", "enable", "provide", "retract"])
        try:
            if op == "load":
                name = rng.choice(sorted(SPECS))
                if any(item.name == name for item in ctx.plugins):
                    continue
                await ctx.load_plugin(_make(name))
            elif op in ("unload", "disable"):
                if not ctx.plugins:
                    continue
                name = rng.choice([item.name for item in ctx.plugins])
                await getattr(ctx, f"{op}_plugin")(name)
            elif op == "enable":
                candidates = [
                    item.name for item in ctx.plugins if item.status in ("disabled", "failed")
                ]
                if not candidates:
                    continue
                await ctx.enable_plugin(rng.choice(candidates))
            elif op == "provide":
                key = rng.choice(EXTERNAL)
                if key in ctx.services:
                    continue
                await ctx.provide(key, 1)
            else:
                key = rng.choice(EXTERNAL)
                if key not in ctx.services:
                    continue
                await ctx.retract(key)
        except (
            DuplicatePluginError,
            MissingPluginError,
            DuplicateSlotError,
            MissingServiceError,
            ValueError,
        ):
            pass

        assert ctx.revision >= last_revision
        last_revision = ctx.revision
        assert_steady(ctx)

    assert_steady(ctx)


@pytest.mark.parametrize("seed", [5, 13])
async def test_random_effect_inject_and_service_operations(seed: int) -> None:
    rng = random.Random(seed)
    ctx = Context()
    effect_handles: list[object] = []
    injection_handles: list[object] = []
    seq = 0
    last_revision = 0

    for _ in range(150):
        op = rng.choice(
            ["effect", "unload-effect", "inject", "cancel-inject", "provide", "retract"]
        )
        try:
            if op == "effect":
                seq += 1
                key = f"svc{seq}"

                async def install(k: str = key, i: int = seq) -> None:
                    await ctx.provide(k, i)

                effect_handles.append(await ctx.effect(f"e{seq}", install))
            elif op == "unload-effect":
                if not effect_handles:
                    continue
                handle = effect_handles.pop(rng.randrange(len(effect_handles)))
                await handle()
            elif op == "inject":
                seq += 1
                deps = rng.choice([*EXTERNAL, *(ctx.services or EXTERNAL)])
                out = f"out{seq}"

                async def callback(c: Context, o: str = out) -> None:
                    await c.provide(o, 1)

                injection_handles.append(await ctx.inject([deps], callback))
            elif op == "cancel-inject":
                if not injection_handles:
                    continue
                handle = injection_handles.pop(rng.randrange(len(injection_handles)))
                await handle()
            elif op == "provide":
                key = rng.choice(EXTERNAL)
                if key in ctx.services:
                    continue
                await ctx.provide(key, 1)
            else:
                key = rng.choice(EXTERNAL)
                if key not in ctx.services:
                    continue
                await ctx.retract(key)
        except (DuplicateSlotError, MissingServiceError):
            pass

        assert ctx.revision >= last_revision
        last_revision = ctx.revision
        assert_steady(ctx)

    for handle in effect_handles:
        await handle()
    for handle in injection_handles:
        await handle()
    assert_steady(ctx)
