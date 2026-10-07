"""MiniHarness —— 把前四个练习装配成一个可用的小 harness。

组装视图（谁接谁）：

    provider (llm_seam)            registry + ToolPipeline (tool_pipeline)
        │                                   │  .execute() 满足执行器契约
        └────────────► AgentLoop ◄──────────┘
                          │  on_event 事件流
                          ▼
                    SessionRecorder ──► Session ──► JsonlStore（落盘）
                          │
                          └──► presenter（打印 / 观察者）

四个接缝（本练习要"看见"的东西）：
1. `ToolPipeline.execute` 满足 `AgentLoop` 的 execute_tool 契约 —— 工具管线原样插进
   主循环，循环一行都不用改（_03 的接缝价值，在这里完成组装）。
2. `SessionRecorder` 作为 `AgentLoop` 的 on_event 回调 —— 事件流落进会话日志
   （铁律 #1：模型可见 ⟺ 已记录）。
3. `Session.derive_messages` 提供循环的初始历史 —— **resume 就是把历史从日志投影
   出来喂回循环**，不存在单独的恢复分支。
4. 事件 sink 同时喂给 recorder（先落日志）与 presenter（再打印）—— 日志是真相，
   打印只是观察。

M4 起（P2 `_01`）新增第 5 处：**系统提示词由 section 装配器产出**。`open()` 的
`system_prompt` 参数既可收一段成品文本，也可收一个 `PromptAssembler`；装配结果
（含作用域遮蔽效果）写进 `session/start`，即"模型当时看到的提示词"。装配逻辑本身
在 `harness/prompt/`，本类只负责在装配点调用它。

本类不含业务逻辑，只把上面这些接线固化成"一个对象"。它替代了 _04 的
`Runner`（`Runner` 仍是更底层的胶水，本类在其上再叠一层"工具面 + 审批"）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from harness.agent import TurnResult
from harness.llm.provider import LLMProvider
from harness.llm.vocabulary import Message
from harness.prompt import PromptAssembler, default_assembler
from harness.runner import Runner, open_session
from harness.session import JsonlStore, RepairReport, Session
from harness.tools import ApprovalPolicy, ToolMiddleware, ToolPipeline, ToolRegistry
from harness.tools.workspace import build_workspace_registry

# 观察者回调：kind + payload（与 AgentLoop 的事件一致）。
Presenter = Callable[[str, dict], None]

# system 提示的入参形态（M4）：
#   None              → 用内置默认装配器（default_assembler）装配
#   str               → 直接当作成品提示词（向后兼容 P1 的写法，测试/demo 常用）
#   PromptAssembler   → 调 .assemble() 得到提示词（由 section 装配而来）
SystemPrompt = str | PromptAssembler | None

# 默认系统提示词文本（由内置 section 装配而来）。保留这个常量是为了向后兼容：
# webui/server.py 等地方把它当作默认字符串使用。
DEFAULT_SYSTEM_PROMPT = default_assembler().assemble()


class MiniHarness:
    """一条会话的完整装配：provider + 工具管线 + 主循环 + 会话日志。"""

    def __init__(
        self,
        session: Session,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        approval: ApprovalPolicy | None = None,
        timeout: float = 30.0,
        max_steps: int = 8,
        middlewares: Sequence[ToolMiddleware] = (),
        on_event: Presenter | None = None,
    ) -> None:
        self.session = session
        # 工具管线：注册表 + 审批策略 + 中间件 + 超时，拼成一条执行管线。
        self.pipeline = ToolPipeline(
            registry,
            approval=approval,
            timeout=timeout,
            middlewares=middlewares,
        )
        self._tools = registry.specs()
        # Runner 负责把 AgentLoop 的事件流接进会话日志；presenter 透传给它。
        self._runner = Runner(
            session,
            provider,
            self.pipeline.execute,  # ← 管线通过执行器契约插进主循环
            self._tools,
            max_steps=max_steps,
            on_event=on_event,
        )

    # ------------------------------------------------------------------
    # 使用
    # ------------------------------------------------------------------

    def send(self, user_text: str) -> TurnResult:
        """处理一个 turn：一条用户输入 → （可能的工具往返）→ 最终回答。"""
        return self._runner.send(user_text)

    @property
    def messages(self) -> list[Message]:
        """当前模型历史（由日志投影而来 —— 权威视图）。"""
        return self._runner.messages

    @property
    def history(self) -> list[Message]:
        """主循环在内存里持有的历史（在线执行的真实结果，用于同构对照）。"""
        return self._runner.history

    @property
    def tools(self):
        """当前可见工具的说明书（装配进模型请求）。"""
        return list(self._tools)

    # ------------------------------------------------------------------
    # 便捷构造
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        session_id: str,
        *,
        provider: LLMProvider,
        root: str | Path,
        workspace: str | Path | None = None,
        system_prompt: SystemPrompt = None,
        approval: ApprovalPolicy | None = None,
        include_search: bool = False,
        timeout: float = 30.0,
        max_steps: int = 8,
        middlewares: Sequence[ToolMiddleware] = (),
        on_event: Presenter | None = None,
    ) -> tuple[MiniHarness, RepairReport]:
        """打开（或恢复）一条会话并装好工具面。

        对新的 session_id 是"创建"，对已存在的 session_id 是"恢复"——同一条路径。
        返回 (harness, 修复报告)。report.repaired 为真表示上次被强杀、尾部已修复。

        system_prompt（M4）：
        - None            用内置默认装配器（由 section 装配出默认提示词）；
        - str             直接当成品提示词（向后兼容；测试与 demo 常显式传一段文本）；
        - PromptAssembler 调 `.assemble()` 得到提示词——这是 M4 的主路径：
                          传入的装配器可以带作用域覆盖，装配结果（连同遮蔽效果）
                          会被写进 session/start 事件，成为"模型当时看到的提示词"。
        """
        store = JsonlStore(Path(root))
        # 把入参形态归一成一段文本（装配点就在这一行）。
        prompt_text = _resolve_system_prompt(system_prompt)
        session, report = open_session(session_id, store, system_prompt=prompt_text)
        registry = build_workspace_registry(
            Path(workspace) if workspace is not None else Path(root),
            include_search=include_search,
        )
        harness = cls(
            session,
            provider,
            registry,
            approval=approval,
            timeout=timeout,
            max_steps=max_steps,
            middlewares=middlewares,
            on_event=on_event,
        )
        return harness, report


def _resolve_system_prompt(system_prompt: SystemPrompt) -> str:
    """把 system_prompt 的三种入参形态归一成一段文本。

    单独抽成模块级函数：这段"入参归一"的逻辑与 MiniHarness 的装配无关，
    放在类外让 open() 保持清爽，也便于单测直接验算。
    """
    if system_prompt is None:
        return default_assembler().assemble()
    if isinstance(system_prompt, PromptAssembler):
        return system_prompt.assemble()
    return system_prompt


__all__ = ["MiniHarness", "Presenter", "DEFAULT_SYSTEM_PROMPT", "SystemPrompt"]
