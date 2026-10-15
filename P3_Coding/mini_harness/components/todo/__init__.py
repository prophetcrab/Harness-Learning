"""todo 构件 —— 会话级任务清单（`_07`）。

- `domain.py`：纯领域（条目类型 / 三条约束 / 整表折叠 / 摘要）
- `store.py`：把清单写进会话日志的服务面（校验在前，写在后）

与 goal（`_06`）的分工写在 domain 的模块说明里：goal 是"做完是什么样"，
todo 是"接下来做哪几步"。
"""

from components.todo.command import (
    USAGE as TODO_USAGE,
)
from components.todo.command import (
    TodoCommandResult,
    format_todos,
    run_todo_command,
)
from components.todo.domain import (
    MAX_IN_PROGRESS,
    MAX_TODOS,
    TODO_STATUSES,
    TodoError,
    TodoItem,
    TodoStatus,
    fold_todos,
    normalize,
    summarize,
)
from components.todo.store import TodoStore

__all__ = [
    "MAX_IN_PROGRESS",
    "MAX_TODOS",
    "TODO_STATUSES",
    "TODO_USAGE",
    "TodoCommandResult",
    "TodoError",
    "TodoItem",
    "TodoStatus",
    "TodoStore",
    "fold_todos",
    "format_todos",
    "run_todo_command",
    "normalize",
    "summarize",
]
