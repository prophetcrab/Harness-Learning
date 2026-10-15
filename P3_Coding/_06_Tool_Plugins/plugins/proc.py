"""proc 插件 —— 提供命令执行能力（`subprocess` 服务）。

两个实现（同一个 `SubprocessService` 协议）：

    local_proc_plugin()               真实子进程（超时杀整棵进程树；凭证剥除）
    scripted_proc_plugin(results)     剧本回放（零真实进程；测试/演示默认）

`subprocess` 是**可选依赖**：工具面（tools 插件）用 `ctx.get` 取它，没有就
不注册 shell 工具——"可选能力"的写法与"必需依赖用 inject"相对照。
"""

from __future__ import annotations

from collections.abc import Iterable

from components.proc import CommandResult, LocalSubprocess, ScriptedSubprocess, SubprocessService
from kernel import Context
from plugins.services import SUBPROCESS_SERVICE


class ProcPlugin:
    """把一个 SubprocessService 实现装成 `subprocess` 服务。"""

    #: 应用角色（base）：命令执行是 coding 的核心能力之一。
    category = "base"
    def __init__(self, service: SubprocessService) -> None:
        self._service = service
        self.name = f"proc:{type(service).__name__}"

    def setup(self, ctx: Context) -> None:
        ctx.provide(SUBPROCESS_SERVICE, self._service)


def local_proc_plugin() -> ProcPlugin:
    """真实子进程执行。"""
    return ProcPlugin(LocalSubprocess())


def scripted_proc_plugin(results: Iterable[CommandResult] | None = None) -> ProcPlugin:
    """剧本回放：缺省给一条"假装跑成功"的结果。"""
    script = list(results) if results is not None else [CommandResult(exit_code=0, stdout="(剧本)")]
    return ProcPlugin(ScriptedSubprocess(script))


__all__ = ["ProcPlugin", "local_proc_plugin", "scripted_proc_plugin"]
