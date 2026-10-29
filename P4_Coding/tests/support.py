"""测试共用工具：造插件、装载、收敛后不变量检查。"""

from __future__ import annotations

from typing import Any

from core import Context


def make_plugin(
    name: str,
    *,
    inject: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    provides: tuple[str, ...] = (),
    values: dict[str, Any] | None = None,
    log: list[str] | None = None,
    setup_error: BaseException | None = None,
    cleanup_error: BaseException | None = None,
) -> object:
    """造一个最小插件。

    `provides` 的服务在 setup 里提供（值默认是插件名，可用 `values` 覆盖）；
    `log` 记录 `<name>:setup` / `<name>:cleanup`；两个 error 用来造失败路径。
    """
    payload = values or {}

    class Plugin:
        pass

    Plugin.name = name  # type: ignore[attr-defined]
    Plugin.inject = inject  # type: ignore[attr-defined]
    Plugin.optional = optional  # type: ignore[attr-defined]
    Plugin.requires = requires  # type: ignore[attr-defined]

    async def setup(self: object, ctx: Context) -> object:
        for key in provides:
            await ctx.provide(key, payload.get(key, name))
        if log is not None:
            log.append(f"{name}:setup")
        if setup_error is not None:
            raise setup_error

        def cleanup() -> None:
            if log is not None:
                log.append(f"{name}:cleanup")
            if cleanup_error is not None:
                raise cleanup_error

        return cleanup

    Plugin.setup = setup  # type: ignore[attr-defined]
    return Plugin()


async def load(ctx: Context, *plugins: object) -> None:
    for plugin in plugins:
        await ctx.load_plugin(plugin)


def info(ctx: Context, name: str):
    """取某个插件的状态快照。"""
    return next(item for item in ctx.plugins if item.name == name)


def assert_steady(ctx: Context) -> None:
    """收敛后世界必须满足的不变量（每次操作后都可以检查）。"""
    services = set(ctx.services)
    effects = set(ctx.effects)
    names = [item.name for item in ctx.plugins]
    assert len(names) == len(set(names)), f"台账里有重名插件：{names}"

    for item in ctx.plugins:
        if item.status == "active":
            assert not item.missing, f"{item.name} active 却缺依赖 {item.missing}"
            assert f"plugin:{item.name}" in effects, f"{item.name} active 却没有自己的 effect"
            assert set(item.provided) <= services, (
                f"{item.name} 声称提供的服务不在服务表里："
                f"{sorted(set(item.provided) - services)}"
            )
        else:
            assert f"plugin:{item.name}" not in effects, (
                f"{item.name} 是 {item.status} 却还挂着 effect"
            )
        if item.status == "pending":
            assert item.missing, f"{item.name} pending 却依赖齐备（应已被激活）"
        if item.status == "disabled":
            assert item.provided == ()

    for injection in ctx.injections:
        if injection.status == "active":
            assert all(dep in services for dep in injection.deps), (
                f"{injection.name} active 却缺依赖 {injection.missing}"
            )
