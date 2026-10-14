"""goal 领域 —— 一个会话的持久目标：类型、不变量与重放折叠（纯逻辑）。

**一个会话只有一个"当前目标"**（对齐 dsh `packages/goal`）。目标不是"待办清单"，
而是**完成条件**：一件事被说清楚了、要跨很多轮才能做完，才值得建目标。
日常单轮的事不该建目标——这条写进工具描述里，靠模型自己判断。

四条性质（前三条对应 dsh 的设计，第四条是本练习的地基）：

1. **状态由日志派生，不单独存储**。所有变更都是 `goal/change` 事件，当前目标由
   `fold_goal()` 从事件流重放出来——和消息一样，"日志是唯一真相，目标是它的视图"。
   于是它天然跨 turn、跨进程重启、跨 fork 存活，不需要任何额外持久化。
2. **变更带版本号（CAS）**。每次变更 `revision + 1`；调用方要带上它看到的
   revision，对不上就拒绝（"你看的是旧版本，先重新读"）。避免模型拿着过期视图
   改错东西。
3. **阶段流转有明确许可表**（`ALLOWED_TRANSITIONS`），非法流转直接报错。
   `complete` 是终态（只能被 clear）；未完成的目标必须先暂停/完成/清掉，
   才能建下一个；**已完成的目标可以直接被替换**。
4. **续跑许可是进程内的，不进日志**（`activation`）。"要不要自动继续"是运行时的
   决定，不是目标本身的属性——重启后默认回到 disarmed，不会自己跑起来。

对应 dsh：`packages/goal/goal/src/{types,fold,invariant}.ts`。
差异说明：dsh 把状态机拆在 runtime/invariant 两处、用 Branded 类型区分 id；
本练习用一个模块装下全部纯逻辑（类型 + 折叠 + 许可表），够用且更好读。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from components.session.events import Event

#: 目标的生命阶段。complete 是终态。
GoalPhase = Literal["active", "paused", "blocked", "complete"]

#: 变更操作（=`goal/change` 事件的 operation 字段）。
GoalOperation = Literal["create", "edit", "pause", "resume", "complete", "block", "clear"]

GOAL_PHASES: tuple[str, ...] = ("active", "paused", "blocked", "complete")
GOAL_OPERATIONS: tuple[str, ...] = (
    "create", "edit", "pause", "resume", "complete", "block", "clear",
)

#: 轮次上限的默认值。**这是防跑飞的安全阀**：目标激活后会自动续跑 Round，
#: 没有上限的话一个想不明白的目标能烧掉一整天。dsh 默认 256；本练习取 8——
#: 小到肉眼可查，又不至于几步就停。（创建时可以显式指定更大的值。）
DEFAULT_MAX_GOAL_ROUNDS = 8

#: 阶段许可表：`操作 → 允许的起始阶段`。不在表里的流转一律拒绝（fail loud）。
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "edit": ("active", "paused", "blocked"),        # 目标内容可改；完成之后冻结
    "pause": ("active",),
    "resume": ("paused", "blocked"),                # 解封也是 resume
    "complete": ("active", "paused", "blocked"),
    "block": ("active",),
}

#: 续跑消息的来源标记（进 `user/message` 的 source 字段）。
#: 折叠靠它数"这个目标已经跑了几轮"——人说的话不算轮次，只有自动续跑算。
GOAL_ROUND_SOURCE = "goal-round"

#: 自动续跑时替用户说的那句话（它只负责"推一把"，具体背景在目标与历史里）。
CONTINUE_PROMPT = (
    "继续推进当前目标。完成后调用 update_goal 把目标标记为 complete；"
    "如果确实被卡住了就标记为 blocked 并说明原因；"
    "如果目标已经不需要了就 clear_goal 清掉。"
)


class GoalError(ValueError):
    """目标操作被拒绝（不变量或 CAS 失败）。消息面向模型/用户，要能直接读懂。"""


@dataclass(frozen=True)
class GoalBlockReason:
    """被卡住的原因：一个机器可路由的码 + 一句人话。"""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class GoalSnapshot:
    """一条 `goal/change` 事件里携带的完整目标（变更后的样子）。

    `revision` 从 1 开始，每次变更 +1——它是 CAS 的依据。
    """

    id: str
    revision: int
    objective: str
    phase: GoalPhase
    max_goal_rounds: int = DEFAULT_MAX_GOAL_ROUNDS
    blocked_reason: GoalBlockReason | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase,
            "max_goal_rounds": self.max_goal_rounds,
        }
        if self.blocked_reason is not None:
            data["blocked_reason"] = self.blocked_reason.to_dict()
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GoalSnapshot:
        """严格解码：缺字段/类型不对当场报错（日志被改过就不该假装能读）。"""
        try:
            reason_raw = raw.get("blocked_reason")
            reason = (
                GoalBlockReason(code=str(reason_raw["code"]), message=str(reason_raw["message"]))
                if reason_raw is not None
                else None
            )
            snapshot = cls(
                id=str(raw["id"]),
                revision=int(raw["revision"]),
                objective=str(raw["objective"]),
                phase=str(raw["phase"]),          # type: ignore[arg-type]
                max_goal_rounds=int(raw["max_goal_rounds"]),
                blocked_reason=reason,
            )
        except (KeyError, TypeError) as exc:
            raise GoalError(f"goal/change 负载不合法：{raw!r}（{exc}）") from exc
        if snapshot.phase not in GOAL_PHASES:
            raise GoalError(f"未知阶段：{snapshot.phase}（合法：{GOAL_PHASES}）")
        if snapshot.revision < 1:
            raise GoalError(f"revision 必须是正整数，收到 {snapshot.revision}")
        if snapshot.phase == "blocked" and snapshot.blocked_reason is None:
            raise GoalError("blocked 阶段必须带 blocked_reason")
        if snapshot.phase != "blocked" and snapshot.blocked_reason is not None:
            raise GoalError(f"{snapshot.phase} 阶段不该带 blocked_reason")
        return snapshot


@dataclass(frozen=True)
class GoalProjection:
    """折叠出来的当前目标（视图层；`activation` 不在内——它是进程内的）。"""

    goal: GoalSnapshot
    rounds_started: int          # 已通过"续跑"跑掉的轮数
    created_at: str              # 创建事件的写入时刻
    updated_at: str              # 最近一次变更的写入时刻

    @property
    def id(self) -> str:
        return self.goal.id

    @property
    def phase(self) -> GoalPhase:
        return self.goal.phase

    @property
    def objective(self) -> str:
        return self.goal.objective

    @property
    def max_goal_rounds(self) -> int:
        return self.goal.max_goal_rounds

    @property
    def revision(self) -> int:
        return self.goal.revision

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.goal.to_dict(),
            "rounds_started": self.rounds_started,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class _FoldState:
    """重放累加器（fold_goal 内部用；外面看不到）。"""

    goal: GoalSnapshot | None = None
    rounds_started: int = 0
    created_at: str = ""
    updated_at: str = ""
    seen_ids: set[str] = field(default_factory=set)


def _apply_change(state: _FoldState, event: Event) -> None:
    """把一条 `goal/change` 折进累加器（每个分支都严格校验，坏日志当场炸）。"""
    data = event.data
    operation = str(data.get("operation", ""))
    if operation not in GOAL_OPERATIONS:
        raise GoalError(f"未知的 goal 变更操作：{operation}（合法：{GOAL_OPERATIONS}）")

    if operation == "clear":
        if state.goal is not None:
            state.seen_ids.add(state.goal.id)
        state.goal = None
        state.rounds_started = 0
        state.updated_at = event.ts
        return

    raw_goal = data.get("goal")
    if not isinstance(raw_goal, dict):
        raise GoalError(f"{operation} 变更缺少 goal 负载：{data!r}")
    incoming = GoalSnapshot.from_dict(raw_goal)

    if operation == "create":
        if incoming.id in state.seen_ids:
            raise GoalError(f"目标 id 被重复使用：{incoming.id}（id 必须唯一）")
        if incoming.revision != 1:
            raise GoalError(f"create 的 revision 必须是 1，收到 {incoming.revision}")
        if state.goal is not None and state.goal.phase != "complete":
            raise GoalError(
                "已有未完成的目标——先 pause/complete/clear 它，再建新的"
                f"（当前：{state.goal.id} / {state.goal.phase}）"
            )
        state.goal = incoming
        state.rounds_started = 0
        state.created_at = event.ts
        state.updated_at = event.ts
        return

    current = state.goal
    if current is None:
        raise GoalError(f"{operation} 需要先有一个当前目标（现在没有）")
    if incoming.id != current.id:
        raise GoalError(f"{operation} 的目标 id 与当前目标不符：{incoming.id} ≠ {current.id}")
    if incoming.revision != current.revision + 1:
        raise GoalError(
            f"{operation} 的 revision 必须递增 1：收到 {incoming.revision}，"
            f"期望 {current.revision + 1}（CAS 失败——你看到的是旧版本）"
        )
    allowed = ALLOWED_TRANSITIONS.get(operation, ())
    if current.phase not in allowed:
        raise GoalError(
            f"不能从 {current.phase} 执行 {operation}（允许的起始阶段：{allowed or '无'}）"
        )
    state.goal = incoming
    state.updated_at = event.ts


def fold_goal(events: Iterable[Event]) -> GoalProjection | None:
    """把事件流重放成"当前目标"；没有目标（从未创建 / 已 clear）时返回 None。

    顺带数轮次：`user/message` 里 source 为 `goal-round` 的那些，落在当前目标
    创建之后、且目标仍是 active 时，算这个目标跑过的一轮。

    严格性：任何不合法的变更序列（坏负载、乱序 revision、非法流转）都抛
    `GoalError`——坏日志宁可炸在读取处，也不要静默折出一个假目标。
    """
    state = _FoldState()
    for event in events:
        if event.type == "goal/change":
            _apply_change(state, event)
        elif event.type == "user/message":
            if (
                state.goal is not None
                and state.goal.phase == "active"
                and event.data.get("source") == GOAL_ROUND_SOURCE
            ):
                state.rounds_started += 1
    if state.goal is None:
        return None
    return GoalProjection(
        goal=state.goal,
        rounds_started=state.rounds_started,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


def validate_transition(operation: str, current: GoalSnapshot | None) -> None:
    """变更前的许可检查（服务层在写事件之前先问一次，给出更友好的报错）。

    `fold_goal` 里那一份是**重放期**的兜底（防坏日志）；这一份是**写入期**的
    前置校验（防写坏）——两道都有意义，不是重复。
    """
    if operation not in GOAL_OPERATIONS:
        raise GoalError(f"未知操作：{operation}")
    if operation == "clear":
        return
    if operation == "create":
        if current is not None and current.phase != "complete":
            raise GoalError(
                f"已有未完成的目标（{current.id} / {current.phase}）——"
                "先 edit / pause / complete / clear 它"
            )
        return
    if current is None:
        raise GoalError(f"没有当前目标，无法 {operation}")
    allowed = ALLOWED_TRANSITIONS.get(operation, ())
    if current.phase not in allowed:
        raise GoalError(
            f"目标当前是 {current.phase}，不能 {operation}"
            f"（允许的起始阶段：{'、'.join(allowed) or '无'}）"
        )


def check_revision(expected: int | None, current: GoalSnapshot | None) -> None:
    """CAS 检查：调用方声明的 revision 必须与当前一致。"""
    if expected is None:
        return
    actual = current.revision if current is not None else 0
    if int(expected) != actual:
        raise GoalError(
            f"目标已被改动（你现在看到的是 revision {actual}，你带的是 {expected}）"
            "——先 get_goal 重新读一次再改"
        )


def next_snapshot(
    current: GoalSnapshot | None,
    *,
    operation: GoalOperation,
    objective: str | None = None,
    max_goal_rounds: int | None = None,
    block_reason: GoalBlockReason | None = None,
    new_id: str | None = None,
) -> GoalSnapshot:
    """算出变更**之后**的目标快照（纯函数；不碰日志、不碰服务）。

    create 用 `new_id`；其余操作在 `current` 基础上改——revision 加一，
    阶段按操作落位。这是"变更长什么样"的唯一出口，服务层与测试共用它。
    """
    validate_transition(operation, current)

    if operation == "create":
        if not new_id:
            raise GoalError("create 需要一个目标 id")
        objective = (objective or "").strip()
        if not objective:
            raise GoalError("目标的 objective 不能为空")
        rounds = int(max_goal_rounds or DEFAULT_MAX_GOAL_ROUNDS)
        if rounds < 1:
            raise GoalError(f"max_goal_rounds 必须是正整数，收到 {rounds}")
        return GoalSnapshot(
            id=str(new_id), revision=1, objective=objective,
            phase="active", max_goal_rounds=rounds,
        )

    assert current is not None  # validate_transition 已保证
    revision = current.revision + 1
    phase: GoalPhase = current.phase
    reason = current.blocked_reason
    text = current.objective
    rounds = current.max_goal_rounds

    if operation == "edit":
        if objective is not None:
            text = objective.strip()
            if not text:
                raise GoalError("objective 不能改成空串")
        if max_goal_rounds is not None:
            rounds = int(max_goal_rounds)
            if rounds < 1:
                raise GoalError(f"max_goal_rounds 必须是正整数，收到 {rounds}")
    elif operation == "pause":
        phase = "paused"
    elif operation == "resume":
        phase = "active"
    elif operation == "complete":
        phase = "complete"
    elif operation == "block":
        phase = "blocked"
        reason = block_reason or GoalBlockReason(code="unspecified", message="未说明原因")
    else:  # pragma: no cover - 未知操作已在上面的校验里拦下
        raise GoalError(f"未知操作：{operation}")

    if phase != "blocked":
        reason = None
    return GoalSnapshot(
        id=current.id, revision=revision, objective=text,
        phase=phase, max_goal_rounds=rounds, blocked_reason=reason,
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CONTINUE_PROMPT",
    "DEFAULT_MAX_GOAL_ROUNDS",
    "GOAL_OPERATIONS",
    "GOAL_PHASES",
    "GOAL_ROUND_SOURCE",
    "GoalBlockReason",
    "GoalError",
    "GoalOperation",
    "GoalPhase",
    "GoalProjection",
    "GoalSnapshot",
    "check_revision",
    "fold_goal",
    "next_snapshot",
    "validate_transition",
]
