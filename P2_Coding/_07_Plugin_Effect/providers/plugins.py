"""providers 侧的插件 —— 把两条能力接缝装进 ctx（注册即 effect）。

本模块是 `_07` 的"接线样板"：`_04`–`_06` 里用 `ServiceContainer` 手工登记能力，
现在换成插件协议——每个安装函数 `install_*(ctx, …)` 做一件事：
`ctx.provide(FS_CAPABILITY, …)` / `provide(SUBPROCESS_CAPABILITY, …)`，把能力
实现放进命名槽位。

**不需要写撤销动作**：`Context.provide` 在装载期会自动把"撤销槽位"登记为
disposer（注册即 effect 的字面落实）——插件卸载、装载失败、进程退出前的整体
卸载，槽位都会自动回卷。插件作者只需"声明我提供了什么"。

三个安装函数（也可用 make_*_plugin 包成标准插件对象）：

    install_fs(ctx, provider)                  fs 能力（LocalFS/MemoryFS/Jail 由调用方给）
    install_subprocess(ctx, service)           命令执行能力
    install_toolbox(ctx, ...)                  等 fs/子进程槽位就绪后，把工具面
                                               （build_toolbox 的产物）也登记进 ctx

"工具面从哪读能力"由此变成一个明确的点：`install_toolbox` 只 `ctx.require` 槽位——
**槽位没提供就 fail loud**（挂载顺序错误在装载时炸，而不是运行到一半才发现）。
"""

from __future__ import annotations

from pathlib import Path

from kernel import Context
from providers.filesystem import FileSystem
from providers.service import FS_CAPABILITY, SUBPROCESS_CAPABILITY
from providers.subprocess import SubprocessService
from providers.toolbox import build_toolbox

# 工具面在 ctx 里的槽位名（会话装配从这里取）。
TOOLBOX_CAPABILITY = "toolbox"


def install_fs(ctx: Context, provider: FileSystem) -> None:
    """把一个 FileSystem 实现装进 ctx（撤销由 ctx 的 effect 自动管理）。"""
    ctx.provide(FS_CAPABILITY, provider)


def install_subprocess(ctx: Context, service: SubprocessService) -> None:
    """把一个 SubprocessService 实现装进 ctx（撤销由 ctx 自动管理）。"""
    ctx.provide(SUBPROCESS_CAPABILITY, service)


def install_toolbox(
    ctx: Context,
    *,
    workspace: str | Path | None = None,
    include_search: bool = False,
    shell_timeout: float = 30.0,
    shell_max_output_chars: int = 20_000,
) -> None:
    """从 ctx 里**已提供**的能力组装工具面，并把产物登记进 ctx。

    - fs 槽位必给；subprocess 槽位可选（没有就不装 shell 工具）；
    - 组装参数（workspace / 期限 / 输出上限）在这里显式给出——与 `_06` 的
      "callers own deadlines"同一条纪律；
    - 工具面登记进 TOOLBOX_CAPABILITY；卸载时随 effect 自动回卷。
    """
    fs = ctx.require(FS_CAPABILITY)  # 缺了就 fail loud（装载顺序错误当场暴露）
    shell_service = ctx.get(SUBPROCESS_CAPABILITY)  # 可选能力
    registry = build_toolbox(
        fs,
        shell_service,
        workspace=workspace,
        include_search=include_search,
        shell_timeout=shell_timeout,
        shell_max_output_chars=shell_max_output_chars,
    )
    ctx.provide(TOOLBOX_CAPABILITY, registry)


def make_fs_plugin(provider: FileSystem):
    """把 install_fs 包成标准插件对象（有 setup(ctx)，可被 load_plugin 装载）。"""

    class FSPlugin:
        name = f"fs:{type(provider).__name__}"

        def setup(self, ctx: Context) -> None:
            install_fs(ctx, provider)

    return FSPlugin()


def make_subprocess_plugin(service: SubprocessService):
    """把 install_subprocess 包成标准插件对象。"""

    class SubprocessPlugin:
        name = f"subprocess:{type(service).__name__}"

        def setup(self, ctx: Context) -> None:
            install_subprocess(ctx, service)

    return SubprocessPlugin()


def make_toolbox_plugin(**options):
    """把 install_toolbox 包成标准插件对象（组装参数原样透传）。"""

    class ToolboxPlugin:
        name = "toolbox"

        def setup(self, ctx: Context) -> None:
            install_toolbox(ctx, **options)

    return ToolboxPlugin()


def boot_toolbox(
    fs: FileSystem,
    subprocess_service: SubprocessService | None = None,
    *,
    workspace: str | Path | None = None,
    include_search: bool = False,
    shell_timeout: float = 30.0,
    shell_max_output_chars: int = 20_000,
) -> tuple[Context, object]:
    """一条龙的装配：建 ctx → 装载 fs/subprocess/toolbox 三个插件 → 返回 (ctx, 工具面)。

    比手工 `build_toolbox` 多的东西：整条装配**留了撤销通道**——`ctx.unload(...)`
    或直接对每个插件卸载，都会把槽位干净地撤掉（"注册即 effect"的落点）。
    """
    from kernel import load_plugins

    context = Context()
    plugins: list[object] = [make_fs_plugin(fs)]
    if subprocess_service is not None:
        plugins.append(make_subprocess_plugin(subprocess_service))
    plugins.append(
        make_toolbox_plugin(
            workspace=workspace,
            include_search=include_search,
            shell_timeout=shell_timeout,
            shell_max_output_chars=shell_max_output_chars,
        )
    )
    load_plugins(context, plugins)
    return context, context.require(TOOLBOX_CAPABILITY)


__all__ = [
    "TOOLBOX_CAPABILITY",
    "install_fs",
    "install_subprocess",
    "install_toolbox",
    "make_fs_plugin",
    "make_subprocess_plugin",
    "make_toolbox_plugin",
    "boot_toolbox",
]
