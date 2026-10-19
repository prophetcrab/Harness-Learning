"""launcher —— core 的**引导**角色：全仓唯一的组合根（composition root）。

**为什么它在 core 里**（0042）：基座应当**自带启动器**——拿到 core 就能起一个
装配，不需要外部胶水；非插件部分因此只有 core 一处。它和框架模块的区别是
纪律，不是位置：

    core · 框架   不认识任何插件（一律不 import plugins），不出现 harness 词汇
    core · 引导   认识 loader **一个**插件（函数内懒加载，全仓唯一的向上边）；
                  它知道三类角色词表——那是 P4 的产品概念（0041），由它在
                  "起产品"时检查

**引导做的事**（只有这四件）：

    1. 建 Context
    2. 挂上 loader（**唯一特权步骤**——"先有鸡才能装鸡"）
    3. 把装配单交给 loader（整批装配；失败整批回滚）
    4. 若是"起产品"（product=True）→ 产品政策检查 → 返回 Runtime

**产品政策**（只在 product=True 时执行；裸装配 / 空装配单照起）：

    · 每条非分组条目都必须声明 role
    · role 必须是 base / application / capability 之一
    · 至少要有一个**正在运行**的 application 条目（"用户从哪进来"要有答案）

**装配单是数据**（0042）：目前接受 `list[Entry]` / `list[dict]`；文件格式等
出现第二个消费者再定。启动器不读文件、不碰环境变量——那是 M4/M5 的打包层。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.context import Context, MissingPluginError

#: 三类角色词表（P4 的产品概念，见决策 0041）。
#: 只有引导知道它——框架模块里不出现这些词（纯度测试的第二级）。
ROLES: tuple[str, ...] = ("base", "application", "capability")

#: "产品"至少要有的一种角色。
APPLICATION_ROLE = "application"


class BootError(RuntimeError):
    """启动失败（loader 挂不上 / 装配单非法 / 装配失败）。"""


class PolicyError(BootError):
    """产品政策检查未通过（缺角色 / 角色非法 / 没有应用化插件）。"""


@dataclass
class Runtime:
    """一次引导的产物：ctx + 对 loader 的读写入口。

    `ctx` 是**世界本身**（服务、事件、台账都在里面）；`loader` 是行政入口。
    两者都给出去，是因为消费方分得清：测试 / 管理面读 `ctx`，装配与热插拔
    走 `loader`——不存在第三个入口。
    """

    ctx: Context

    @property
    def loader(self) -> Any:
        """当前的 loader 服务（函数内懒加载：引导是 core 唯一允许认识 plugins 的地方）。"""
        from plugins.loader import LOADER_SERVICE

        return self.ctx.require(LOADER_SERVICE)

    def snapshot(self) -> tuple[Any, ...]:
        """条目快照（转发 loader；loader 被停用时 fail loud）。"""
        return self.loader.snapshot()

    def dump(self) -> str:
        """给人看的树报告（`--dump` 的内容；启动器 / 测试 / 排障共用）。"""
        return dump(self)

    def close(self) -> None:
        """卸载世界里的全部插件（逆序；某个清理抛错不打断其余，最后抛第一个）。

        停用中的插件也一并清掉（它们仍占着台账），loader 最后卸——这样树的
        卸载走的是正常入口，而不是"loader 没了、树还挂着"。
        """
        names = [info.name for info in self.ctx.plugins]
        errors: list[BaseException] = []
        for name in reversed(names):
            try:
                self.ctx.unload_plugin(name)
            except MissingPluginError:
                continue  # 已经随级联卸掉了
            except BaseException as exc:  # noqa: BLE001 —— 全部收齐后重抛第一个
                errors.append(exc)
        if errors:
            raise errors[0]


def boot(
    manifest: Sequence[Any] = (),
    *,
    catalog: Mapping[str, Callable[..., object]],
    product: bool = False,
) -> Runtime:
    """起一个装配：建 ctx → 挂 loader → 装装配单 →（可选）产品政策检查。

    - `manifest`：条目列表（Entry 或 dict）；空列表也能起（基座自足）。
    - `catalog`：name → 工厂（loader 的目录）。
    - `product=True`：按三类角色检查这是一份"产品"装配——缺角色、角色非法、
      没有运行中的应用化插件都 fail loud。裸装配不检查（测试 / 嵌入式场景）。

    失败时抛 `AssemblyError`（装配单问题）或 `PolicyError`（产品政策）——
    **不返回半个 Runtime**：失败的装配不留下可用的世界。
    """
    from plugins.loader import LOADER_SERVICE, loader_plugin

    ctx = Context()
    try:
        ctx.load_plugin(loader_plugin(catalog))
        loader = ctx.require(LOADER_SERVICE)
        loader.assemble(manifest)
    except BaseException as exc:
        # 引导失败：把已经装上的（loader 自己）收拾掉，不把半个世界交出去。
        try:
            Runtime(ctx).close()
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
                runtime.close()
            except BaseException:  # 清理失败不遮蔽原始异常
                pass
            raise
    return runtime


def check_product_policy(runtime: Runtime) -> None:
    """产品政策检查（product=True 时调用；也可以单独用来验收一份装配）。

    三条（0042 / 0041）：

        1. 每条**非分组**条目都声明了 role（分组是容器，不需要）；
        2. role 在词表内（写错一个字母不能被当成"别的东西"静默放行）；
        3. 至少一条 role=application 的条目**正在运行**（active）。

    第 3 条用 active 而不是"声明了"：装配成功后 pending 已被 loader 拦下，
    failed 会整批回滚，所以 active 就是这个条目真的起来了。
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
    """把树渲染成一张表（`--dump` 的报告；状态、角色、插件名、缺的依赖）。"""
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
