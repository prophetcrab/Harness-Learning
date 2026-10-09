"""ScriptedSubprocess —— SubprocessService 接缝的剧本实现（Provider 角色）。

按剧本队列回放预设的 `CommandResult`——**不真跑任何进程**。它的地位与
FileSystem 接缝里的 MemoryFS 相同：证明"工具只依赖抽象"（同一套工具测试在
它和 LocalSubprocess 下都成立），让测试不依赖操作系统命令的实际行为与速度。

纪律与 FakeLLM / ScriptedApprover 一致：

- 剧本 = 队列，每次 run() 弹出一条；
- 同时记录收到的**完整请求**（command / cwd / timeout）——测试可以断言
  "工具真的把工作目录和期限传了下来"（消费者职责的一部分）；
- 剧本用完还被调用 → 直接报错（抓住"多跑了一次命令"这类 bug）；
- `assert_all_consumed()` 断言剧本正好用完（抓住"少跑了一次"）。
"""

from __future__ import annotations

from collections.abc import Iterable

from providers.subprocess import CommandResult, SubprocessRequest


class ScriptedSubprocess:
    """按剧本回复的命令执行实现（SubprocessService 协议实现，零真实进程）。"""

    def __init__(self, results: Iterable[CommandResult]) -> None:
        self._script: list[CommandResult] = list(results)
        # 请求记录：每次 run() 存一份（command/cwd/timeout），供测试断言。
        self.requests: list[SubprocessRequest] = []

    @property
    def request_count(self) -> int:
        """被调用过的次数。"""
        return len(self.requests)

    @property
    def remaining(self) -> int:
        """剧本里还剩几条结果。"""
        return len(self._script)

    def assert_all_consumed(self) -> None:
        """断言剧本正好用完（没有"该跑的命令没跑到"）。"""
        if self._script:
            raise AssertionError(
                f"子进程剧本还剩 {self.remaining} 条没用：有命令没有被执行到。"
            )

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """按剧本回放一次；记录请求；剧本用完还调用则报错。"""
        self.requests.append(SubprocessRequest(command=command, cwd=cwd, timeout=timeout))
        if not self._script:
            raise AssertionError(
                f"子进程剧本已用完：这是第 {self.request_count} 次调用，"
                f"但剧本只准备了 {self.request_count - 1} 条结果。"
                "说明有代码跑了预期之外的一次命令。"
            )
        return self._script.pop(0)


__all__ = ["ScriptedSubprocess"]
