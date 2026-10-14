"""goal 服务构件 —— 把目标变更写进会话日志（构件层；不认识插件）。

**职责边界**：本模块只管"目标是什么、怎么变、写到哪"；**不碰循环**。
"要不要自动续跑"是插件层 + 循环的事（见 plugins/goal.py）。

三个要点：

1. **写事件，不写文件**。每次变更往会话日志追加一条 `goal/change`，携带
   **变更后的完整快照**。当前目标由 `fold_goal()` 重放出来——没有第二份存储，
   所以不存在"状态和日志对不上"的可能。
2. **写之前先校验**（许可 + CAS + 负载合法），校验失败抛 `GoalError`，
   日志一个字节都不动。宁可拒绝，不要写一条坏事件进去——它会毒死之后每一次重放。
3. **变更后广播**（可选）：`on_change` 回调让上层把新状态推给界面。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from components.goal.domain import (
    DEFAULT_MAX_GOAL_ROUNDS,
    GoalBlockReason,
    GoalError,
    GoalOperation,
    GoalProjection,
    GoalSnapshot,
    check_revision,
    fold_goal,
    next_snapshot,
    validate_transition,
)
from components.session.events import Event


def new_goal_id() -> str:
    """一个新的目标 id（用户/模型都不需要自己编；避免撞 id）。"""
    return f"goal-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class GoalChange:
    """一次成功的变更：事件 + 变更后的快照（给界面推流用）。"""

    operation: GoalOperation
    snapshot: GoalSnapshot | None      # clear 时为 None
    revision: int                      # clear 时为 0


class GoalStore:
    """会话日志之上的目标操作面（薄，但把校验顺序钉死）。

    对上层（插件 / 工具）暴露的全是**语义动作**，不是"写一条事件"——
    调用方说"完成这个目标"，而不是"往日志里塞 operation=complete"。
    """

    def __init__(
        self,
        *,
        append: Callable[[str, dict[str, Any]], Event],
        load_events: Callable[[], Iterable[Event]],
        on_change: Callable[[GoalChange], None] | None = None,
    ) -> None:
        self._append = append
        self._load_events = load_events
        self._on_change = on_change

    # ---- 读 ----

    def current(self) -> GoalProjection | None:
        """当前目标（由日志现折；没有则为 None）。"""
        return fold_goal(self._load_events())

    def view(self) -> dict[str, Any]:
        """给模型/界面看的读视图（无目标时 `{"goal": null}`）。"""
        projection = self.current()
        return {"goal": projection.to_dict() if projection is not None else None}

    # ---- 写（全部先校验后落日志） ----

    def _commit(self, operation: GoalOperation, snapshot: GoalSnapshot | None) -> GoalChange:
        data: dict[str, Any] = {"operation": operation}
        if snapshot is not None:
            data["goal"] = snapshot.to_dict()
        self._append("goal/change", data)
        change = GoalChange(
            operation=operation,
            snapshot=snapshot,
            revision=snapshot.revision if snapshot is not None else 0,
        )
        if self._on_change is not None:
            self._on_change(change)
        return change

    def create(
        self,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
        goal_id: str | None = None,
    ) -> GoalChange:
        """建一个目标（active）。已有未完成目标时拒绝。"""
        current = self.current()
        validate_transition("create", current.goal if current else None)
        snapshot = next_snapshot(
            None, operation="create", objective=objective,
            max_goal_rounds=max_goal_rounds if max_goal_rounds is not None
            else DEFAULT_MAX_GOAL_ROUNDS,
            new_id=goal_id or new_goal_id(),
        )
        return self._commit("create", snapshot)

    def edit(
        self,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
        expected_revision: int | None = None,
    ) -> GoalChange:
        """改目标内容（objective / 轮次上限）。完成之后不许改。"""
        current = self._require_current()
        check_revision(expected_revision, current.goal)
        if objective is None and max_goal_rounds is None:
            raise GoalError("edit 至少要改一样（objective 或 max_goal_rounds）")
        snapshot = next_snapshot(
            current.goal, operation="edit",
            objective=objective, max_goal_rounds=max_goal_rounds,
        )
        return self._commit("edit", snapshot)

    def pause(self, *, expected_revision: int | None = None) -> GoalChange:
        return self._simple("pause", expected_revision)

    def resume(self, *, expected_revision: int | None = None) -> GoalChange:
        """恢复（也用于给 blocked 的目标解封）。"""
        return self._simple("resume", expected_revision)

    def complete(self, *, expected_revision: int | None = None) -> GoalChange:
        """标记完成（终态）。"""
        return self._simple("complete", expected_revision)

    def block(
        self,
        *,
        code: str = "unspecified",
        message: str = "未说明原因",
        expected_revision: int | None = None,
    ) -> GoalChange:
        """标记被卡住（带机器可路由的码 + 人话）。"""
        current = self._require_current()
        check_revision(expected_revision, current.goal)
        snapshot = next_snapshot(
            current.goal, operation="block",
            block_reason=GoalBlockReason(code=code, message=message),
        )
        return self._commit("block", snapshot)

    def clear(self) -> GoalChange:
        """清掉当前目标（留一条 clear 事件；目标 id 不会被回收复用）。"""
        current = self.current()
        validate_transition("clear", current.goal if current is not None else None)
        return self._commit("clear", None)

    # ---- 内部 ----

    def _simple(self, operation: GoalOperation, expected_revision: int | None) -> GoalChange:
        current = self._require_current()
        check_revision(expected_revision, current.goal)
        snapshot = next_snapshot(current.goal, operation=operation)
        return self._commit(operation, snapshot)

    def _require_current(self) -> GoalProjection:
        current = self.current()
        if current is None:
            raise GoalError("当前没有目标——先用 create_goal 建一个")
        return current


__all__ = ["GoalChange", "GoalStore", "new_goal_id"]
