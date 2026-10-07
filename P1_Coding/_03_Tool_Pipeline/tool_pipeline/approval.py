"""审批策略 —— 危险工具放行前问一句"能不能做"，问不到人就 fail-closed 拒绝。

设计要点（为什么是"策略接口"而不是散落的 input()）：

1. **接口化**：管线只依赖 `ApprovalPolicy.approve()`，不关心应答者是人、脚本
   还是测试替身。换审批方式 = 换一个实现，管线代码不动。
2. **fail-closed**：**问不到应答方（非交互环境、EOF）时一律拒绝**，而不是
   默认放行。"拒绝"的代价是任务没做完；"默认放行"的代价可能是删库/执行任意命令。
3. **可测试**：`ScriptedApprover` 用剧本队列驱动，测试无需真人也能断言
   "拒绝后工具没有被执行"。

对应 dsh：`packages/interaction/user-approval/src/index.ts`（审批是拦截式的
交互服务，无应答方时按策略 fail-closed）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class ApprovalRequest:
    """一次审批请求：谁、要做什么、为什么被拦。"""

    tool_name: str
    arguments: dict
    reason: str = ""


@dataclass(frozen=True)
class ApprovalDecision:
    """审批结论。reason 在拒绝时会回填给模型（错误是给模型的输入）。"""

    allowed: bool
    reason: str = ""


@runtime_checkable
class ApprovalPolicy(Protocol):
    """审批策略协议。任何提供 approve() 的对象都满足它。"""

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        ...


# ---------------------------------------------------------------------------
# 实现
# ---------------------------------------------------------------------------


class AutoApprove:
    """总是同意（--no-approve、自动化演示用）。"""

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, "自动放行")


class AutoDeny:
    """总是拒绝（演示拒绝路径、CI 里禁止危险操作）。"""

    def __init__(self, reason: str = "策略拒绝：当前环境禁止该操作") -> None:
        self._reason = reason

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(False, self._reason)


class ScriptedApprover:
    """按剧本队列回复（测试用）；剧本用完还问则报错。

    与 FakeLLM 同样的纪律：剧本数量必须与"被问到的次数"精确匹配，
    这样才能抓住"管线多问了一次 / 少问了一次"这类 bug。
    """

    def __init__(self, decisions: Iterable[ApprovalDecision]) -> None:
        self._script: list[ApprovalDecision] = list(decisions)
        self.requests: list[ApprovalRequest] = []

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        if not self._script:
            raise AssertionError(
                f"审批剧本已用完：这是第 {len(self.requests)} 次询问，"
                "说明管线在不该问审批的时候问了审批。"
            )
        return self._script.pop(0)

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def assert_all_consumed(self) -> None:
        if self._script:
            raise AssertionError(
                f"审批剧本还剩 {len(self._script)} 条没用：该问的审批没问到。"
            )


class PromptApprover:
    """终端 y/n 审批；无交互终端时 fail-closed 拒绝。

    input_fn / interactive 可注入，便于离线测试：把 interactive=False 就能
    断言"非交互环境一律拒绝"。
    """

    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        interactive: bool = True,
        yes: tuple[str, ...] = ("y", "yes"),
    ) -> None:
        self._input = input_fn
        self._interactive = interactive
        self._yes = yes

    def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        if not self._interactive:
            return ApprovalDecision(
                False,
                "无应答方（非交互环境），按 fail-closed 策略拒绝",
            )
        prompt = (
            f"\n[审批] 工具「{request.tool_name}」申请执行：{request.arguments}\n"
            f"       原因：{request.reason or '该操作被标记为需要审批'}\n"
            "       是否允许？[y/N] "
        )
        try:
            answer = self._input(prompt)
        except EOFError:
            return ApprovalDecision(False, "审批输入中断（EOF），按 fail-closed 拒绝")
        if answer.strip().lower() in self._yes:
            return ApprovalDecision(True, "用户批准")
        return ApprovalDecision(False, "用户拒绝执行该工具")
