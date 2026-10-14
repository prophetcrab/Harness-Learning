"""goal 插件 —— 持久目标（`_06`）：服务 + 模型工具 + 人命令 + 续跑闸门。

一个会话一个目标，跨 turn / 重启 / fork 存活（靠日志，不靠额外存储）。

三样东西装在这一处：

**① 服务面 `goals`**（给别的插件/工具用）
    GoalStore 之上按会话包一层：`create / edit / pause / resume / complete /
    block / clear / current / view`。每次变更往该会话日志追加 `goal/change`
    （构件层已保证"先校验后落笔"）。

**② 模型工具三件**（`get_goal` / `create_goal` / `update_goal`）
    让模型自己管目标。工具描述里写死了政策：**长程目标才建，单轮小事不建**。
    读工具返回紧凑 JSON（id/revision/阶段/轮次/上限/被卡原因），
    改工具要求带 `expected_revision`（CAS）——模型拿着旧视图改会当场被拒。

**③ `/goal` 人命令**（REPL 与网页命令菜单共用）
    看一眼 / 新建 / 改 / 暂停 / 恢复 / 完成 / 清掉。**人不花模型的钱**：
    `/goal` 直接操作服务，不走 turn。

**续跑闸门（本阶段最容易出事的地方）**

目标处于 active 时，一条新的人消息进来 → 跑完这一轮后，如果目标还活着且
还有轮次预算，就**自动再跑一轮**（`user/message` 带 `source="goal-round"`）。

三道闸门，缺一不可：

1. **轮次上限**（`max_goal_rounds`，默认 8）：每续一轮先检查 `rounds_started`，
   超了就把目标标成 `blocked`（原因码 `rounds-exhausted`），不再续——
   **这是防跑飞的核心保险**：没有它，一个想不明白的目标能烧掉一整天。
2. **取消即停**：`loop.cancel()` / `should_stop` 一旦触发，本轮收尾后不再续。
3. **只在 active 续**：paused / blocked / complete / 没有目标 → 都不续。

对应 dsh：`packages/goal`（goal 服务 / tool-goal / command-goal / goal-round-driver）。
差异说明：dsh 的续跑由独立的 round-driver 包驱动、有独立的 round 事件与
`activation` 状态机；本练习把它收成一个"跑完检查、该续就再调一次 run"的
简单循环闸门——语义相同（active 才续、有上限、可取消），机制少一层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from components.goal import (
    CONTINUE_PROMPT,
    GoalError,
    GoalProjection,
    GoalStore,
)
from components.tools import define_tool
from kernel import Context
from plugins.events import GOAL_CHANGED, LOOP_EVENT, TURN_END
from plugins.services import (
    APPROVAL_SERVICE,
    CONTINUATION_SERVICE,
    GOALS_SERVICE,
    SESSIONS_SERVICE,
    TOOLS_SERVICE,
)

# ---------------------------------------------------------------------------
# 服务面
# ---------------------------------------------------------------------------


class GoalService:
    """按会话提供目标读写（每条会话一个 GoalStore，日志就是它的存储）。

    `compact` / `RPC` 之类的消费者只认这一层；它们不该知道事件怎么写。
    """

    def __init__(self, ctx: Context, *, default_session: str = "default") -> None:
        self._ctx = ctx
        self._stores: dict[str, GoalStore] = {}
        #: 当前正在跑哪个会话——由 AgentService 在**持锁**时设置。
        #: 为什么需要它：模型工具是**全局注册**的（工具面不区分会话），
        #: 但目标必须落到"这次对话"的日志里。`ask` 持有的那把锁保证同一时刻
        #: 只有一个会话在跑，所以"当前会话"是可靠的（与 `_03` 用量账本
        #: 记 session 的做法一致）。
        self._current_session = default_session

    def set_current_session(self, session_id: str) -> None:
        """入口在开始跑 turn 之前调用（须与 turn 串行，见类文档）。"""
        self._current_session = session_id or self._current_session

    def session_of_record(self) -> str:
        """模型工具该读写哪个会话（= 当前正在跑的那个）。"""
        return self._current_session

    def store(self, session_id: str | None = None) -> GoalStore:
        """取某会话的目标操作面（首次调用时现建，绑定到该会话的日志）。"""
        cached = self._stores.get(session_id)
        if cached is not None:
            return cached
        sessions = self._ctx.get(SESSIONS_SERVICE)
        if sessions is None:
            raise GoalError("sessions 服务未就绪——目标状态需要会话日志才能存活")
        session, _, _ = sessions.open(session_id)

        def on_change(change) -> None:
            self._ctx.emit(
                GOAL_CHANGED,
                session=session_id,
                operation=change.operation,
                goal=change.snapshot.to_dict() if change.snapshot is not None else None,
            )

        store = GoalStore(
            append=session.append,
            load_events=lambda: session.events,
            on_change=on_change,
        )
        self._stores[session_id] = store
        return store

    # ---- 读 ----
    def current(self, session_id: str | None = None) -> GoalProjection | None:
        return self.store(session_id).current()

    def view(self, session_id: str | None = None) -> dict[str, Any]:
        return self.store(session_id).view()

    def active(self, session_id: str | None = None) -> GoalProjection | None:
        """当前目标，且阶段为 active 时返回它（续跑闸门用）。"""
        current = self.current(session_id)
        return current if current is not None and current.phase == "active" else None


# ---------------------------------------------------------------------------
# 续跑闸门
# ---------------------------------------------------------------------------


@dataclass
class _Gate:
    """一条会话的续跑记账（进程内；不进日志）。"""

    rounds_this_turn: int = 0      # 本次续跑循环里已经跑了几轮（防同一次卡死）
    stopped: bool = False          # 取消/停止后置位，之后不再续


class GoalContinuation:
    """把"active 的目标"变成"自动多跑几轮"的闸门（见模块 docstring 的三道闸门）。

    与循环的耦合只有一个方向：**跑完一个 turn 之后问一句"还该继续吗"**。
    循环不认识目标，闸门也不改循环——插件在 `TURN_END` 之后自己再调 `run`。
    """

    def __init__(
        self,
        service: GoalService,
        *,
        on_event=None,
        max_rounds_per_turn: int = 32,
    ) -> None:
        self._service = service
        self._on_event = on_event
        self._max_rounds_per_turn = max_rounds_per_turn
        self._gates: dict[str, _Gate] = {}

    def begin(self, session_id: str) -> None:
        """新一轮人消息开始：重置本会话的续跑记账。"""
        self._gates[session_id] = _Gate()

    def stop(self, session_id: str) -> None:
        self._gates.setdefault(session_id, _Gate()).stopped = True

    def should_continue(self, session_id: str) -> str | None:
        """该不该再跑一轮？返回续跑用的提示词；不该则 None（并把该停的停掉）。

        判定顺序（每一条都对应一个真实会出事的情形）：

        1. 本会话被要求停止（取消）→ 不续；
        2. 本轮已经续太多次（单次循环的硬护栏）→ 不续；
        3. 目标不在 active → 不续（paused/blocked/complete/无目标）；
        4. 轮次预算用尽 → **把目标标成 blocked**（留痕），不续。
        """
        gate = self._gates.setdefault(session_id, _Gate())
        if gate.stopped:
            return None
        if gate.rounds_this_turn >= self._max_rounds_per_turn:
            return None
        goal = self._service.active(session_id)
        if goal is None:
            return None
        if goal.rounds_started >= goal.max_goal_rounds:
            # 预算用尽：标 blocked 并说明——**留痕比静默停止重要**，
            # 否则用户只看到"它自己不动了"，不知道是为什么。
            try:
                self._service.store(session_id).block(
                    code="rounds-exhausted",
                    message=(
                        f"目标已跑 {goal.rounds_started} 轮，达到上限 "
                        f"{goal.max_goal_rounds} 轮。确认后 resume 或提高上限再继续。"
                    ),
                )
            except GoalError:
                pass   # 已被别处改过（比如刚好 complete）：不续就是了
            return None
        gate.rounds_this_turn += 1
        return CONTINUE_PROMPT


# ---------------------------------------------------------------------------
# 模型工具
# ---------------------------------------------------------------------------

GET_GOAL_DESCRIPTION = (
    "读取本会话的当前目标：id、版本号、目标内容、阶段（active/paused/blocked/"
    "complete）、已续跑轮数、轮数上限、被卡原因。"
    "**改目标之前先调用它**——update_goal 需要带上这里看到的 revision。"
)

CREATE_GOAL_DESCRIPTION = (
    "为本会话创建一个持久目标（跨轮次、跨重启都记得）。"
    "**只在用户的请求是一件需要很多轮才能做完的长程任务时才建**"
    "（例如「把迁移全做完」「把文档里所有失败的检查项修好」）；"
    "日常单轮的小事不要建。一个会话同时只能有一个未完成的目标。"
)

UPDATE_GOAL_DESCRIPTION = (
    "更新当前目标。action 取值：\n"
    "  edit      改目标内容（objective / max_goal_rounds）\n"
    "  pause     暂停（不再自动续跑，但目标还在）\n"
    "  resume    恢复（也用于给 blocked 的目标解封）\n"
    "  complete  目标完成（终态）\n"
    "  block     被卡住了——必须说明 reason_code 与 message\n"
    "  clear     清掉目标（不需要了）\n"
    "`expected_revision` 是乐观锁：带上你从 get_goal 看到的版本号，"
    "对不上会被拒绝（说明目标已被改过）。"
)


def _goal_tools(service: GoalService):
    """构造三件工具：读 / 建 / 改。返回实现函数，交给 registry.register。

    **会话是现取的**（`service.session_of_record()`）而不是构造时定死：
    工具面是全局的，而目标要落到"这次对话"的日志里。模型在 g1 对话里调
    create_goal，目标就该进 g1——定死成 "default" 会写错地方（实测踩到）。
    """

    def get_goal(**_ignored: Any) -> dict[str, Any]:
        """读当前目标。多带参数也容忍（模型偶尔会画蛇添足）。"""
        return service.view(service.session_of_record())

    def create_goal(
        objective: str = "", max_goal_rounds: int | None = None, **_ignored: Any
    ) -> dict[str, Any]:
        try:
            session = service.session_of_record()
            change = service.store(session).create(
                objective, max_goal_rounds=max_goal_rounds,
            )
        except GoalError as exc:
            return {"error": str(exc)}
        return {"ok": True, "operation": change.operation, **service.view(session)}

    def update_goal(
        action: str,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
        reason_code: str | None = None,
        message: str | None = None,
        expected_revision: int | None = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        session = service.session_of_record()
        store = service.store(session)
        try:
            if action == "edit":
                store.edit(
                    objective=objective, max_goal_rounds=max_goal_rounds,
                    expected_revision=expected_revision,
                )
            elif action == "pause":
                store.pause(expected_revision=expected_revision)
            elif action == "resume":
                store.resume(expected_revision=expected_revision)
            elif action == "complete":
                store.complete(expected_revision=expected_revision)
            elif action == "block":
                store.block(
                    code=reason_code or "unspecified",
                    message=message or "未说明原因",
                    expected_revision=expected_revision,
                )
            elif action == "clear":
                store.clear()
            else:
                return {"error": f"未知 action：{action}（见工具描述里的取值）"}
        except GoalError as exc:
            return {"error": str(exc)}
        return {"ok": True, "action": action, **service.view(session)}

    return get_goal, create_goal, update_goal


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class GoalPlugin:
    """提供 `goals` 服务 + 三件模型工具（inject tools / sessions）。"""

    name = "goal"
    inject = (TOOLS_SERVICE, SESSIONS_SERVICE)
    #: 审批策略可选：本插件不用它，但续跑可能与审批交互（装了就在）。
    optional = (APPROVAL_SERVICE,)

    def __init__(
        self,
        *,
        default_session: str = "default",
        max_rounds_per_turn: int = 32,
        register_tools: bool = True,
    ) -> None:
        self._default_session = default_session
        self._max_rounds_per_turn = max_rounds_per_turn
        self._register_tools = register_tools

    def setup(self, ctx: Context):
        service = GoalService(ctx, default_session=self._default_session)
        continuation = GoalContinuation(
            service, max_rounds_per_turn=self._max_rounds_per_turn,
        )
        ctx.provide(GOALS_SERVICE, service)

        # 续跑闸门的记账。**重置由 AgentService.ask 显式调用**（人工入口），
        # 不订阅 turn/start——续跑的 turn 也会发 turn/start，订阅它会每轮把
        # "单次循环护栏"清零，等于没有护栏（实测踩到，会一直跑下去）。
        def on_turn_end(**payload: Any) -> None:
            session = str(payload.get("session") or self._default_session)
            # 取消/预算用尽这类收尾之后不续（停止信号优先）
            status = str(payload.get("status") or "")
            if status in ("cancelled", "budget_exceeded"):
                continuation.stop(session)

        def on_loop_event(**payload: Any) -> None:
            # 循环事件里带 stream_cancelled：用户中途取消 → 别再自动续
            if payload.get("kind") == "stream_cancelled":
                continuation.stop(str(payload.get("session") or self._default_session))

        ctx.on(TURN_END, on_turn_end)
        ctx.on(LOOP_EVENT, on_loop_event)

        # 续跑闸门也借一个服务槽暴露出去：入口（chat.py / webui）拿它决定
        # "这一轮跑完之后要不要接着跑"——它不属于 goals 服务（那是纯状态面），
        # 所以单开一个槽，含义清楚：这是"要不要继续"的决策者。
        ctx.provide(CONTINUATION_SERVICE, continuation)

        # 模型工具（装载期注册 → 卸载时注销：外部对象由插件自己撤）
        if not self._register_tools:
            return None
        registry = ctx.require(TOOLS_SERVICE)
        get_goal, create_goal, update_goal = _goal_tools(service)
        registry.register(define_tool("get_goal", GET_GOAL_DESCRIPTION), get_goal)
        registry.register(define_tool("create_goal", CREATE_GOAL_DESCRIPTION), create_goal)
        registry.register(define_tool("update_goal", UPDATE_GOAL_DESCRIPTION), update_goal)

        def dispose() -> None:
            for name in ("get_goal", "create_goal", "update_goal"):
                registry.unregister(name)

        return dispose


def goal_plugin(
    *,
    default_session: str = "default",
    max_rounds_per_turn: int = 32,
    register_tools: bool = True,
) -> GoalPlugin:
    """工厂：`register_tools=False` 只提供服务与闸门（测试/极简场景）。"""
    return GoalPlugin(
        default_session=default_session,
        max_rounds_per_turn=max_rounds_per_turn,
        register_tools=register_tools,
    )


__all__ = [
    "CREATE_GOAL_DESCRIPTION",
    "GET_GOAL_DESCRIPTION",
    "UPDATE_GOAL_DESCRIPTION",
    "GoalContinuation",
    "GoalPlugin",
    "GoalService",
    "goal_plugin",
]
