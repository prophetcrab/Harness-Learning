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

from difflib import get_close_matches
from pathlib import Path

from config import ConfigError
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


# =========================================================================
# `_08`：LLM 能力的插件化 + 配置树激活（配置 → 插件 → ctx）
# =========================================================================

# LLM 能力（与 fs/subprocess 并列的第三个槽位；`_08` 把它也纳入配置可换）。
LLM_CAPABILITY = "llm"


def install_llm(ctx: Context, provider) -> None:
    """把一个 LLMProvider 装进 ctx（撤销自动管理）。"""
    ctx.provide(LLM_CAPABILITY, provider)


def make_llm_plugin(provider):
    """把 install_llm 包成标准插件对象。"""

    class LlmPlugin:
        name = f"llm:{type(provider).__name__}"

        def setup(self, ctx: Context) -> None:
            install_llm(ctx, provider)

    return LlmPlugin()


# ---------------------------------------------------------------------------
# 工厂注册表：配置行 name → 构造插件的函数
#
# 工厂签名：factory(config: dict, stage_root: Path) -> 插件对象
#   - config 是该行的 config 字典（来自配置树，已过类型校验）；
#   - stage_root 是阶段目录（相对路径按它解析、.env 从项目根读）；
#   - 工厂只**构造**（校验配置、new 对象），不激活——激活统一由 boot_tree 做，
#     于是"先校验全部配置、再统一激活"能把错误拦在任何副作用之前。
# ---------------------------------------------------------------------------


def _resolve(cfg_value: str | None, default: str, stage_root: Path) -> Path:
    """把配置里的路径解析到阶段目录下（相对路径按 stage_root 解析）。"""
    path = Path(cfg_value) if cfg_value else Path(default)
    return path if path.is_absolute() else (stage_root / path)


def _factory_fs_local(config: dict, stage_root: Path):
    from providers.local import LocalFS

    root = _resolve(config.get("root"), "demo_workspace/ws", stage_root)
    root.mkdir(parents=True, exist_ok=True)  # 本地 provider 的工作区先建好
    return make_fs_plugin(LocalFS(root))


def _factory_fs_memory(config: dict, stage_root: Path):
    from providers.memory import MemoryFS

    return make_fs_plugin(MemoryFS())


def _factory_fs_jail(config: dict, stage_root: Path):
    from providers.jail import WorkspaceJailFS
    from providers.local import LocalFS

    root = _resolve(config.get("root"), "demo_workspace/ws", stage_root)
    root.mkdir(parents=True, exist_ok=True)
    return make_fs_plugin(WorkspaceJailFS(LocalFS(root)))


def _factory_subprocess_local(config: dict, stage_root: Path):
    from providers.subprocess_local import LocalSubprocess

    return make_subprocess_plugin(LocalSubprocess())


def _factory_subprocess_scripted(config: dict, stage_root: Path):
    from providers.subprocess import CommandResult
    from providers.subprocess_scripted import ScriptedSubprocess

    raw_results = config.get("results")
    if raw_results is None:
        results = [  # 默认剧本：三条足够演示三种结局
            CommandResult(0, "演示输出：命令正常结束。\n"),
            CommandResult(1, "", "演示输出：命令以退出码 1 结束。\n"),
            CommandResult(None, "演示输出：跑到一半被超时终止。\n", timed_out=True),
        ]
    else:
        results = [
            CommandResult(
                exit_code=item.get("exit_code"),
                stdout=str(item.get("stdout", "")),
                stderr=str(item.get("stderr", "")),
                timed_out=bool(item.get("timed_out", False)),
            )
            for item in raw_results
        ]
    return make_subprocess_plugin(ScriptedSubprocess(results))


def _factory_llm_fake(config: dict, stage_root: Path):
    from harness.llm import FakeLLM, text_reply, tool_call_reply

    # 剧本覆盖 demo/chat 常见的三个 turn：算数（工具）、命令（工具）、写文件（工具）。
    script = [
        tool_call_reply("calculate", {"expression": "1234*56.78"}, call_id="call_calc"),
        text_reply("1234 × 56.78 = 70066.52。（来自 dev profile 的 FakeLLM）"),
        tool_call_reply("shell", {"command": "echo demo"}, call_id="call_sh"),
        text_reply("命令已执行（剧本回放：没有真跑）。（dev profile）"),
        tool_call_reply("write_file", {"path": "notes/todo.txt", "content": "dev 笔记"}, call_id="call_w"),
        text_reply("已写入 notes/todo.txt（MemoryFS：只在内存里）。（dev profile）"),
        text_reply("（离线剧本：dev profile 不联网）"),
    ]
    return make_llm_plugin(FakeLLM(script))


def _factory_llm_deepseek(config: dict, stage_root: Path):
    import os

    from harness.env import load_env
    from harness.llm.deepseek import DeepSeekProvider

    load_env(stage_root.parents[1])  # 项目根 .env
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "profile 选择了 llm:deepseek，但未找到 DEEPSEEK_API_KEY。\n"
            "配置：项目根目录 .env 写入 DEEPSEEK_API_KEY=sk-...\n"
            "离线体验请用 dev profile。"
        )
    # 优先级：配置 > 环境变量 > 内置默认（显式解析：行里配了什么一目了然）。
    model = config.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    base_url = config.get("base_url") or os.environ.get(
        "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
    )
    return make_llm_plugin(DeepSeekProvider(api_key=api_key, model=model, base_url=base_url))


def _factory_toolbox(config: dict, stage_root: Path):
    workspace = _resolve(config.get("workspace"), "demo_workspace/ws", stage_root)
    return make_toolbox_plugin(
        workspace=workspace,
        include_search=bool(config.get("include_search", False)),
        shell_timeout=float(config.get("shell_timeout", 30.0)),
        shell_max_output_chars=int(config.get("shell_max_output_chars", 20_000)),
    )


#: 插件工厂注册表：行的 name 是这里的键；未知 name 在激活前 fail loud。
PLUGIN_FACTORIES = {
    "fs:local": _factory_fs_local,
    "fs:memory": _factory_fs_memory,
    "fs:jail": _factory_fs_jail,
    "subprocess:local": _factory_subprocess_local,
    "subprocess:scripted": _factory_subprocess_scripted,
    "llm:fake": _factory_llm_fake,
    "llm:deepseek": _factory_llm_deepseek,
    "toolbox": _factory_toolbox,
}

#: 每个插件接受的 config 键（`_09` 的"未知键报错"）——键拼错（`shell_timeou`）
#: 不再静默失效，激活前直接指出来。空集合 = 该插件无参数。
PLUGIN_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "fs:local": frozenset({"root"}),
    "fs:memory": frozenset(),
    "fs:jail": frozenset({"root"}),
    "subprocess:local": frozenset(),
    "subprocess:scripted": frozenset({"results"}),
    "llm:fake": frozenset(),
    "llm:deepseek": frozenset({"model", "base_url"}),
    "toolbox": frozenset(
        {"workspace", "include_search", "shell_timeout", "shell_max_output_chars"}
    ),
}


def _check_config_keys(row_id: str, name: str, config: dict) -> None:
    """校验行的 config 键都是该插件认识的（未知键 fail loud + 拼写建议）。"""
    allowed = PLUGIN_CONFIG_KEYS.get(name, frozenset())
    unknown = [key for key in config if key not in allowed]
    if unknown:
        close = get_close_matches(unknown[0], sorted(allowed), n=1, cutoff=0.6)
        hint = f"；你是不是想写 {close[0]!r}？" if close else ""
        allowed_text = "、".join(sorted(allowed)) if allowed else "（该插件无参数）"
        raise ConfigError(
            f"行 {row_id!r}（{name}）有未知参数：{unknown}（可用：{allowed_text}）{hint}"
        )


def build_plugins_from_tree(tree, *, stage_root: Path) -> list[object]:
    """把配置树（激活行）构造成插件对象列表——**校验阶段，不产生任何副作用**。

    未知 name / 未知参数键 / 参数类型错都在这一步暴露（在装载之前）。
    错误消息带**行 id**（`_09` 的错误定位）；可疑的名字/键给拼写建议。
    """
    plugins: list[object] = []
    for row in tree.active_rows():
        factory = PLUGIN_FACTORIES.get(row.name)
        if factory is None:
            known = sorted(PLUGIN_FACTORIES)
            close = get_close_matches(row.name, known, n=1, cutoff=0.55)
            hint = f"；你是不是想写 {close[0]!r}？" if close else ""
            raise ConfigError(
                f"行 {row.id!r} 的 name 未知：{row.name!r}"
                f"（可用：{'、'.join(known)}）{hint}"
            )
        _check_config_keys(row.id, row.name, row.config)
        try:
            plugins.append(factory(row.config, stage_root))
        except ConfigError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            # 工厂里的参数错误（如 shell_timeout: "很快"）→ 加行定位再抛（fail loud）。
            raise ConfigError(
                f"行 {row.id!r}（{row.name}）的参数有问题：{exc}"
            ) from exc
    return plugins


def boot_tree(tree, *, stage_root: Path) -> Context:
    """把配置树激活成一个 ctx：构造全部插件 → 统一装载（失败自动回卷已装部分）。

    两步分离的意义：配置错误（未知 name、非法参数、缺 key）在**构造阶段**就
    全部拦下，此时没有任何东西被登记；装载阶段的失败（如 toolbox 找不到 fs 槽位）
    则由 kernel 的 load_plugins 回卷——ctx 永远不残留半装状态。
    """
    from kernel import load_plugins

    context = Context()
    plugins = build_plugins_from_tree(tree, stage_root=stage_root)
    load_plugins(context, plugins)
    return context


__all__ = [
    "LLM_CAPABILITY",
    "PLUGIN_FACTORIES",
    "TOOLBOX_CAPABILITY",
    "boot_tree",
    "boot_toolbox",
    "build_plugins_from_tree",
    "install_fs",
    "install_llm",
    "install_subprocess",
    "install_toolbox",
    "make_fs_plugin",
    "make_llm_plugin",
    "make_subprocess_plugin",
    "make_toolbox_plugin",
]
