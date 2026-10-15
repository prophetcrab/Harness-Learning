"""todo 构件 —— 会话级任务清单（`_06`）。

- `domain.py`：纯领域（条目类型 / 三条约束 / 整表折叠 / 摘要）
- `store.py`：把清单写进会话日志的服务面（校验在前，写在后）
- `command.py`：`/todo` 的解析与排版（网页与 REPL 共用一份）

**清单是"执行步骤"，不是"完成条件"**：模型自己拆、边干边勾，**不驱动任何
自动续跑**（"持续推进"是 goal 类机制的事）。理由见 domain 的模块说明。

一个容易踩的语义：**`/todo` 是记事本，不是开工按钮**——它只读写清单、永远不调
模型。让模型干活要么 `/todo run`（把清单交给它执行），要么直接把需求发成消息。
"""

from components.todo.command import (
    USAGE as TODO_USAGE,
)
from components.todo.command import (
    TodoCommandResult,
    build_run_prompt,
    format_todos,
    is_run_request,
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
    "build_run_prompt",
    "fold_todos",
    "format_todos",
    "is_run_request",
    "normalize",
    "run_todo_command",
    "summarize",
]
