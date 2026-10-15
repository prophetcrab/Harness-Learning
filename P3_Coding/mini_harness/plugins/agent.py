"""agent 插件 —— 会话服务（`agent` 服务）：把一切接成"能对话的 agent"。

这是消费者中的消费者：注入 `llm / tools / sessions / prompt` 四个服务，
组装方式和 P1 `_05` / P2 的 `MiniHarness` 完全一致（构件的接线语义不变）：

    provider（llm 服务）
        └─► RuntimePromptProvider      每 step 重新渲染系统提示词（prompt 服务）
                └─► AgentLoop          主循环（turn/step）
                        ├─ execute_tool ─► ToolPipeline（审批 + 超时 + 中间件）
                        └─ on_event ──► SessionRecorder ──► Session ──► JSONL

**打开即恢复**（P1 语义原样保留）：`ask(session_id, text)` 对不存在的会话是
"创建"、对已存在的是"恢复"——同一条路径（装载日志 → 投影历史 → 建循环）。
新会话把"开场那一刻的渲染 + 装配单"写进 `session/start`；此后每 step 渲染
若变化，追加 `system/message`（后一条遮蔽前一条，日志只增不改）。

**本插件相对 P2 的差异**（路线图里"P3 完成、P4 重表达"的提前兑现）：
- 会话不再由 `MiniHarness.open()` 手工装配，而是 `ctx.require(...)` 四个服务；
- 审批策略是构造参数（默认 fail-closed），入口按场景给（终端 y/n / 自动放行）；
- 每条会话的组件在 `_open` 里现接——**换掉任意插件，新建会话立即用新组合**。

`session_id` 相同则复用缓存（同一场对话 turn 接续）；缓存以服务为单位，
卸载本插件时随 effect 一起消失（下次装载重新打开）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

from components.agent import AgentLoop, TurnResult
from components.llm import LLMProvider
from components.prompt import PromptRenderer
from components.prompt.provider import RuntimePromptProvider
from components.session import Session, effective_system_prompt
from components.session.recorder import SessionRecorder
from components.tools import ApprovalPolicy, ToolMiddleware, ToolPipeline, ToolRegistry
from kernel import Context
from plugins.events import LOOP_EVENT, TURN_END, TURN_START
from plugins.services import (
    AGENT_SERVICE,
    APPROVAL_SERVICE,
    COMPACTER_SERVICE,
    LLM_SERVICE,
    PROMPT_SERVICE,
    SESSIONS_SERVICE,
    TOOLS_SERVICE,
)

# 观察者回调：主循环事件的 kind + payload（chat 用它把过程打给人看）。
LoopObserver = Callable[[str, dict], None]


class HarnessSession:
    """一条已打开的会话：日志 + 主循环 + 管线（同一个 session_id 下复用）。"""

    def __init__(
        self,
        session: Session,
        loop: AgentLoop,
        pipeline: ToolPipeline,
        registry: ToolRegistry,
        *,
        resumed: bool,
        repaired: bool,
        compactor: Any | None = None,
    ) -> None:
        self.session = session
        self.loop = loop
        self.pipeline = pipeline
        self.registry = registry
        self.resumed = resumed
        self.repaired = repaired
        #: 本会话装配时的压缩器（None = 这个会话不压缩）。换压缩插件后新建
        #: 会话立即用新组合；已打开的会话保持打开那一刻的装配（P2 的语义）。
        self.compactor = compactor

    @property
    def messages(self):
        """当前模型历史（由日志投影而来——权威视图）。"""
        return self.session.derive_messages()

    def ask(self, text: str) -> TurnResult:
        """跑一个 turn：一条用户输入排空到最终回答。"""
        return self.loop.run(text, tools=self.registry.specs())

    def compact(self) -> Any | None:
        """手动压缩一次（`/compact`）：跳过阈值、保住最后一轮，返回结果或 None。

        抛异常是**有意**的（没有压缩器 / 摘要调用失败）——这是用户按下的动作，
        调用方（入口）应当把原因回给用户，而不是像自动路径那样悄悄跳过。
        """
        return self.loop.compact_now(self.registry.specs(), manual=True)


class AgentService:
    """会话管理器：按 session_id 打开/缓存/复用会话（对外唯一入口）。

    **并发粒度：每条会话一把锁**（`_05` 收尾改）。原先是一把全局 `RLock` 把
    整个 turn 包住——A 会话跑 30 秒，B 会话就得在门口干等，尽管两条会话的
    历史、循环、压缩器完全不共享。现在：

    - 打开/查表用一把**短临界区**的全局锁（只护几个 dict，不做慢活）；
    - 真正跑 turn 时持**这条会话自己的锁**——同会话串行（历史是那一份对象，
      两个 turn 交叉写必然乱），**不同会话并行**。

    **观察者分两层**（并行之后必须能挂多个）：

    - `open(on_event=...)`：主观察者，**替换**语义（终端入口 `chat.py` 每轮
      重新打开时用；与 P1/P2 行为一致）；
    - `watch(...)`：追加订阅，返回退订函数（界面推流器用——页面刷新后新旧
      两个连接会短暂并存，**不能互相顶掉**）。
    """

    def __init__(
        self,
        ctx: Context,
        *,
        provider: LLMProvider,
        renderer: PromptRenderer,
        sessions,
        registry: ToolRegistry,
        approval: ApprovalPolicy | None = None,
        timeout: float = 30.0,
        max_steps: int | None = None,
        token_budget: int | None = None,
        middlewares: Sequence[ToolMiddleware] = (),
    ) -> None:
        self._ctx = ctx
        self._provider = provider
        self._renderer = renderer
        self._sessions = sessions
        self._registry = registry
        self._approval = approval
        self._timeout = timeout
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._middlewares = tuple(middlewares)
        self._open: dict[str, HarnessSession] = {}
        #: 主观察者（open(on_event=...) 设置，**替换**语义）。
        self._observers: dict[str, LoopObserver] = {}
        #: 追加订阅者（watch() 挂，**可多个**：并行/刷新后新旧连接会并存）。
        self._watchers: dict[str, list[LoopObserver]] = {}
        #: 每条会话一把锁：同会话串行、跨会话并行（见类文档）。
        self._turn_locks: dict[str, threading.RLock] = {}
        #: 全局短锁：只护上面几个 dict 的读写，绝不包住 turn。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 会话打开（打开即恢复）
    # ------------------------------------------------------------------

    def open(self, session_id: str = "default", *, on_event: LoopObserver | None = None) -> HarnessSession:
        """打开（或恢复）一条会话；同名复用缓存。返回开场信息（resumed/repaired）。

        `on_event` 是**主观察者**（替换语义）：终端入口每轮重开时用它接管推流；
        界面那种"多个推流器并存"的场景请用 `watch()`。
        """
        with self._lock:
            return self._open_locked(session_id, on_event=on_event)

    def watch(self, session_id: str, observer: LoopObserver) -> Callable[[], None]:
        """追加一个订阅者，返回退订函数。

        **为什么不能复用 open(on_event=...)**：那是替换语义，而并行改造之后
        "同一会话同时有多个观察者"是常态（页面刷新：新连接先挂上，旧连接还要
        收尾；RPC 跟随与界面可能同时在看）。替换会让先来的悄悄失去推送。
        """
        with self._lock:
            self._watchers.setdefault(session_id, []).append(observer)

        def unwatch() -> None:
            with self._lock:
                watchers = self._watchers.get(session_id)
                if watchers and observer in watchers:
                    watchers.remove(observer)

        return unwatch

    def _open_locked(
        self, session_id: str, *, on_event: LoopObserver | None = None
    ) -> HarnessSession:
        if on_event is not None:
            self._observers[session_id] = on_event
        cached = self._open.get(session_id)
        if cached is not None:
            return cached

        # 1) 开场渲染：新会话用它写 session/start；resume 时先取日志里的生效文本作基线。
        trace = self._renderer.render_traced()
        session, resumed, report = self._sessions.open(
            session_id,
            system_prompt=trace.text,
            prompt_trace=trace.to_dict(),
        )
        baseline = trace.text
        if resumed:
            recorded = effective_system_prompt(session.events)
            baseline = recorded if recorded is not None else trace.text

        # 2) 每 step 渲染的包装 provider：变化才记 system/message（带装配单）。
        def on_render(new_trace) -> None:
            session.append(
                "system/message",
                {"content": new_trace.text, "prompt_trace": new_trace.to_dict()},
            )

        provider = RuntimePromptProvider(
            self._provider, self._renderer, on_render=on_render, initial=baseline
        )

        # 3) 事件流：先落日志（唯一真相），再给观察者，再广播到内核事件总线。
        recorder = SessionRecorder(session)

        def sink(kind: str, payload: dict) -> None:
            recorder(kind, payload)
            # 观察者**每次现取**（不在这里捕获）：观察者是"当前这个 turn 的发起方"
            # （如界面某次请求的推流器），会随每个 turn 更换。曾经在这里捕获
            # 第一次打开时的观察者——第二轮起就会往**已经关掉的旧连接**上推流，
            # 写死套接字抛 OSError 把 turn 打断（"只聊了一轮就死"的根因）。
            with self._lock:
                observer = self._observers.get(session_id)
                watchers = list(self._watchers.get(session_id, ()))
            if observer is not None:
                observer(kind, payload)
            for watch in watchers:
                try:
                    watch(kind, payload)
                except Exception:  # noqa: BLE001 —— 一个订阅者断了不影响本次 turn
                    continue
            self._ctx.emit(LOOP_EVENT, session=session_id, kind=kind, payload=payload)

        # 4) 工具管线 + 主循环（历史由日志投影——不存在单独的恢复分支）。
        # 审批策略：显式传入的优先；否则用 ctx 里的 `approval` 服务
        # （plugins/web_approval.py 提供——界面场景下就是"在浏览器里点批准"）；
        # 两者都没有 → None = 管线按 fail-closed 拒绝一切需审批操作。
        approval = self._approval if self._approval is not None else self._ctx.get(APPROVAL_SERVICE)
        pipeline = ToolPipeline(
            self._registry,
            approval=approval,
            timeout=self._timeout,
            middlewares=self._middlewares,
        )
        # 压缩器（`_05`）：从 ctx 现取——没装 compact 插件就是 None（不压缩）。
        compactor = self._ctx.get(COMPACTER_SERVICE)
        loop = AgentLoop(
            provider,
            pipeline.execute,
            max_steps=self._max_steps,
            token_budget=self._token_budget,
            on_event=sink,
            initial_messages=session.derive_messages(),
            initial_turn=session.last_turn_number,
            compactor=compactor,
        )
        opened = HarnessSession(
            session, loop, pipeline, self._registry,
            resumed=resumed, repaired=report.repaired, compactor=compactor,
        )
        self._open[session_id] = opened
        return opened

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    def ask(self, text: str, session_id: str = "default") -> TurnResult:
        """跑一个 turn：打开（或恢复）会话 → 排空输入 → 返回结果。

        **只持这条会话自己的锁**（见类文档）：同一会话的 turn 依次跑（历史是
        那一份对象），**不同会话并行**。全局锁只在 `_open_locked` 里短暂持有。
        """
        with self._lock:
            opened = self._open_locked(session_id)
            turn_lock = self._turn_locks.setdefault(session_id, threading.RLock())
        with turn_lock:
            self._ctx.emit(
                TURN_START,
                session=session_id,
                turn=opened.session.last_turn_number + 1,
                text=text,
            )
            result = opened.ask(text)
            self._ctx.emit(
                TURN_END,
                session=session_id,
                turn=result.turn,
                status=result.status,
                steps=len(result.steps),
                final_text=result.final_text,
            )
            return result

    def cancel(self, session_id: str = "default") -> bool:
        """请求取消这条会话正在跑的 turn（**协作式**：主循环在 step 边界与流式
        分块之间看标记，正在执行的工具调用不会被砍断）。没有已打开的会话 → False。
        """
        with self._lock:
            opened = self._open.get(session_id)
        if opened is None:
            return False
        opened.loop.cancel()
        return True

    def compact(self, session_id: str = "default") -> Any | None:
        """手动压缩一次（`/compact`）：打开（或恢复）会话 → 立即折叠历史。

        与 `ask` 用**同一把会话锁**：压缩会改写内存历史并往日志追加 checkpoint，
        不能让同会话的另一个 turn 在中间穿过去。
        """
        with self._lock:
            opened = self._open_locked(session_id)
            turn_lock = self._turn_locks.setdefault(session_id, threading.RLock())
        with turn_lock:
            return opened.compact()

    @property
    def open_sessions(self) -> list[str]:
        """已缓存的会话 id（按打开顺序）。"""
        return list(self._open)


class AgentPlugin:
    """提供 `agent` 服务（依赖：llm / tools / sessions / prompt）。"""

    name = "agent"
    #: 应用角色（base）：对外唯一入口（ask 一个 turn）。
    category = "base"
    inject = (LLM_SERVICE, TOOLS_SERVICE, SESSIONS_SERVICE, PROMPT_SERVICE)
    #: 可选依赖：取不到就降级（approval 缺失 → 管线 fail-closed；
    #: compacter 缺失 → 不压缩）。声明出来的意义是**台账里看得见**，
    #: 且符合"取过的服务都要声明"这条纪律。
    optional = (APPROVAL_SERVICE, COMPACTER_SERVICE)

    def __init__(
        self,
        *,
        approval: ApprovalPolicy | None = None,
        timeout: float = 30.0,
        max_steps: int | None = None,
        token_budget: int | None = None,
        middlewares: Sequence[ToolMiddleware] = (),
    ) -> None:
        self._approval = approval
        self._timeout = timeout
        self._max_steps = max_steps
        self._token_budget = token_budget
        self._middlewares = tuple(middlewares)

    def setup(self, ctx: Context) -> None:
        service = AgentService(
            ctx,
            provider=ctx.require(LLM_SERVICE),
            renderer=ctx.require(PROMPT_SERVICE),
            sessions=ctx.require(SESSIONS_SERVICE),
            registry=ctx.require(TOOLS_SERVICE),
            approval=self._approval,
            timeout=self._timeout,
            max_steps=self._max_steps,
            token_budget=self._token_budget,
            middlewares=self._middlewares,
        )
        ctx.provide(AGENT_SERVICE, service)


__all__ = ["AgentPlugin", "AgentService", "HarnessSession", "LoopObserver"]
