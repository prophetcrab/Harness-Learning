"""launcher —— 组合根：装配插件，可选地按产品政策检查。

    boot()                  建 Context → 装载 loader 插件 → 装配装配单
                            →（product=True 时）产品政策检查，返回 Runtime
                            （async：`runtime = await boot(...)`）
    check_product_policy()  三条检查：非分组条目必须声明 role；role 必须合法；
                            至少一个 role=application 的条目处于 active
    dump()                  把条目状态渲染成表格

    Runtime                 引导产物：ctx + loader 入口 + close / dump

launcher 认识一个插件：plugins.base.loader（提供 "loader" 服务），在
Runtime.loader 与 boot() 中函数内导入。该插件不在本目录中；缺失时这两个入口
会在导入处抛 ImportError。

loader 契约：`assemble(manifest)` 可以是同步或异步函数（boot 会等待）；
`snapshot()` 保持同步（政策检查与 dump 都是同步读）。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.context import Context, MissingPluginError

#: 三类角色词表（产品政策使用）。
ROLES: tuple[str, ...] = ("base", "application", "capability")

#: 产品政策要求至少有一个该角色的条目处于 active。
APPLICATION_ROLE = "application"


class BootError(RuntimeError):
    """启动失败（loader 挂不上 / 装配单非法 / 装配失败）。"""


class PolicyError(BootError):
    """产品政策检查未通过（缺角色 / 角色非法 / 没有应用化插件）。"""


@dataclass
class Runtime:
    """一次引导的产物：ctx 与 loader 入口。

    ctx 是运行时本身（服务、事件、台账）；loader 负责装配与热插拔。
    """

    ctx: Context

    @property
    def loader(self) -> Any:
        """当前提供 "loader" 服务的插件（函数内导入）。"""
        from plugins.base.loader import LOADER_SERVICE

        return self.ctx.require(LOADER_SERVICE)

    def snapshot(self) -> tuple[Any, ...]:
        """条目快照（转发 loader.snapshot()）。"""
        return self.loader.snapshot()

    def dump(self) -> str:
        """给人看的树报告。"""
        return dump(self)

    async def close(self) -> None:
        """卸载世界里的全部插件（逆序；loader 最后卸）。

        某个清理抛错不打断其余，最后重抛第一个异常。
        """
        names = [info.name for info in self.ctx.plugins]
        errors: list[BaseException] = []
        for name in reversed(names):
            try:
                await self.ctx.unload_plugin(name)
            except MissingPluginError:
                continue  # 已经随级联卸掉了
            except BaseException as exc:  # noqa: BLE001 —— 全部收齐后重抛第一个
                errors.append(exc)
        if errors:
            raise errors[0]


async def boot(
    manifest: Sequence[Any] = (),
    *,
    catalog: Mapping[str, Callable[..., object]],
    product: bool = False,
) -> Runtime:
    """起一个装配：建 Context → 装载 loader → 装配装配单 →（可选）政策检查。

    manifest 是条目列表（Entry 或 dict）；catalog 是 name → 工厂。
    product=True 时执行产品政策检查。`loader.assemble` 可以是同步或异步的。

    失败时清理已装载的插件并抛 BootError（政策失败抛 PolicyError），不返回
    半个 Runtime。
    """
    from plugins.base.loader import LOADER_SERVICE, loader_plugin

    ctx = Context()
    try:
        await ctx.load_plugin(loader_plugin(catalog))
        loader = ctx.require(LOADER_SERVICE)
        result = loader.assemble(manifest)
        if inspect.isawaitable(result):
            await result
    except BaseException as exc:
        # 引导失败：把已经装上的（loader 自己）收拾掉，不把半个世界交出去。
        try:
            await Runtime(ctx).close()
        except BaseException:  # 清理失败不遮蔽原始异常
            pass
        if isinstance(exc, BootError):
            raise
        raise BootError(f"启动失败：{exc}") from exc

    runtime = Runtime(ctx)
    if product:
        try:
            check_product_policy(runtime)
        except BaseException:
            try:
                await runtime.close()
            except BaseException:  # 清理失败不遮蔽原始异常
                pass
            raise
    return runtime


def check_product_policy(runtime: Runtime) -> None:
    """产品政策检查（product=True 时由 boot 调用，也可单独使用）。

    1. 每条非分组条目都声明了 role；
    2. role 在 ROLES 词表内；
    3. 至少一条 role=application 的条目处于 active。
    """
    states = runtime.snapshot()
    unroled = [state.path for state in states if state.name is not None and state.role is None]
    if unroled:
        raise PolicyError(
            f"产品装配要求每条插件条目声明 role（{'/'.join(ROLES)}）："
            f"这些条目没声明：{unroled}"
        )
    unknown = [
        f"{state.path}={state.role!r}"
        for state in states
        if state.role is not None and state.role not in ROLES
    ]
    if unknown:
        raise PolicyError(
            f"角色不合法：{unknown}（可用：{'、'.join(ROLES)}）"
        )
    running_apps = [
        state.path
        for state in states
        if state.role == APPLICATION_ROLE and state.status == "active"
    ]
    if not running_apps:
        declared = [state.path for state in states if state.role == APPLICATION_ROLE]
        hint = f"（声明为 application 的条目：{declared}，但都没在运行）" if declared else ""
        raise PolicyError(
            f"产品至少要有一个正在运行的应用化插件（role={APPLICATION_ROLE}）{hint}"
            "——否则「用户从哪进来」没有答案"
        )


def dump(runtime: Runtime) -> str:
    """把条目状态渲染成表格（路径、角色、状态、插件名 / 缺依赖 / 错误）。"""
    states = runtime.snapshot()
    if not states:
        return "（装配单是空的：只有 loader 在运行）"
    path_width = max(len(state.path) for state in states)
    role_width = max(len(state.role or "-") for state in states)
    lines = [
        f"{'条目':<{path_width}}  {'角色':<{role_width}}  {'状态':<8} 插件 / 说明"
    ]
    for state in states:
        detail = state.plugin or ""
        if state.status == "group":
            detail = "（分组）"
        if state.missing:
            detail = f"缺依赖：{', '.join(state.missing)}"
        if state.error is not None:
            detail = f"{type(state.error).__name__}: {state.error}"
        lines.append(
            f"{state.path:<{path_width}}  {state.role or '-':<{role_width}}  "
            f"{state.status:<8} {detail}".rstrip()
        )
    return "\n".join(lines)


__all__ = [
    "APPLICATION_ROLE",
    "ROLES",
    "BootError",
    "PolicyError",
    "Runtime",
    "boot",
    "check_product_policy",
    "dump",
]
