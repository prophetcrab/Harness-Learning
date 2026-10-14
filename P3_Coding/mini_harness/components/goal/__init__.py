"""goal 构件 —— 一个会话的持久目标（`_06`）。

- `domain.py`：纯领域（类型 / 不变量 / 重放折叠），不碰日志也不碰插件；
- `store.py`：把变更写进会话日志的服务面（校验在前，写在后）。

"要不要自动续跑"不在这里——那是插件层与循环的事（见 plugins/goal.py）。
"""

from components.goal.command import (
    USAGE as GOAL_USAGE,
)
from components.goal.command import (
    GoalCommandResult,
    format_goal,
    run_goal_command,
)
from components.goal.domain import (
    ALLOWED_TRANSITIONS,
    CONTINUE_PROMPT,
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_OPERATIONS,
    GOAL_PHASES,
    GOAL_ROUND_SOURCE,
    GoalBlockReason,
    GoalError,
    GoalOperation,
    GoalPhase,
    GoalProjection,
    GoalSnapshot,
    check_revision,
    fold_goal,
    next_snapshot,
    validate_transition,
)
from components.goal.store import GoalChange, GoalStore, new_goal_id

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CONTINUE_PROMPT",
    "DEFAULT_MAX_GOAL_ROUNDS",
    "GOAL_OPERATIONS",
    "GOAL_PHASES",
    "GOAL_ROUND_SOURCE",
    "GOAL_USAGE",
    "GoalBlockReason",
    "GoalChange",
    "GoalCommandResult",
    "GoalError",
    "GoalOperation",
    "GoalPhase",
    "GoalProjection",
    "GoalSnapshot",
    "GoalStore",
    "check_revision",
    "fold_goal",
    "format_goal",
    "run_goal_command",
    "new_goal_id",
    "next_snapshot",
    "validate_transition",
]
