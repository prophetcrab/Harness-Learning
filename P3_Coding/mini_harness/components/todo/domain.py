"""todo 领域 —— 会话级任务清单：类型、约束与重放折叠（纯逻辑）。

**todo 和 goal 是两种东西**，别混：

    goal   回答"做完是什么样" —— 完成条件，跨很多轮才值得建，会驱动自动续跑
    todo   回答"接下来做哪几步" —— 执行步骤，模型自己拆、边干边勾，不驱动续跑

一个会话一个 goal，但 todo 是一张**清单**。

**整表替换（whole-list），不是增量**（对齐 dsh `todo_write`）：

- 每次写入携带**完整的新列表**，直接替换旧的；没有"改第 3 条"这种操作。
- 于是条目**不需要 id**——没有稳定身份可言，反正整张表是快照。
- 折叠因此简单到极致：**最后一条 `todo/write` 赢**（last-write-wins）。

为什么不做增量：增量意味着"哪条是第几条"要在模型和日志之间对齐，
模型加一条、删一条、重排一下，索引意识很容易错位（"我以为第 2 条是跑测试"）。
整表替换让模型每次都看着完整现状做决定，错不了；代价是 token 略多——
一张清单十几条，值。

**三条约束**（在写入前拒绝，坏数据不进日志）：

1. 内容 trim 后不能为空；
2. 内容不能重复（同一条待办出现两次，说明模型没想清楚）；
3. **同时最多一条 `in_progress`**——这是纪律，不是限制：agent 同一时刻只在一件事上，
   清单的意义才成立。多条并行是人的习惯，不是 agent 该学的。

对应 dsh：`packages/todo/tool-todo/src/types.ts` 与 `index.ts`。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from components.session.events import Event

#: 任务状态三态。complete 是终态，中途不会回退（回退要重写整表）。
TodoStatus = Literal["pending", "in_progress", "completed"]

TODO_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")

#: 同时允许 `in_progress` 的条数上限。1 = 严格串行（本练习的取舍，见模块说明）。
MAX_IN_PROGRESS = 1

#: 清单条数上限（防模型把整个需求文档抄进来）。
MAX_TODOS = 50


class TodoError(ValueError):
    """清单被拒绝（约束不满足）。消息面向模型/用户，要能直接读懂。"""


@dataclass(frozen=True)
class TodoItem:
    """清单里的一条：一句祈使句 + 三态之一。

    故意最小（对齐 dsh）：没有 id、没有优先级、没有进度百分比——
    整表替换之下这些都不需要，加了反而要维护两套真相。
    """

    content: str
    status: TodoStatus = "pending"

    def to_dict(self) -> dict[str, str]:
        return {"content": self.content, "status": self.status}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TodoItem:
        try:
            content = str(raw["content"]).strip()
            status = str(raw.get("status", "pending"))
        except (KeyError, TypeError) as exc:
            raise TodoError(f"todo 条目不合法：{raw!r}（{exc}）") from exc
        if not content:
            raise TodoError("todo 条目的 content 不能为空")
        if status not in TODO_STATUSES:
            raise TodoError(f"未知状态：{status}（合法：{TODO_STATUSES}）")
        return cls(content=content, status=status)  # type: ignore[arg-type]


def normalize(items: Iterable[TodoItem | dict[str, Any]]) -> list[TodoItem]:
    """校验并规整一张清单（写入前的唯一入口，纯函数）。

    三条约束在这里集中执行——服务层、模型工具、人命令都走它，
    于是"什么样的清单算合法"只有一个答案。
    """
    result: list[TodoItem] = []
    seen: set[str] = set()
    for raw in items:
        item = raw if isinstance(raw, TodoItem) else TodoItem.from_dict(raw)
        if item.content in seen:
            raise TodoError(f"清单里有重复条目：{item.content}")
        seen.add(item.content)
        result.append(item)

    if len(result) > MAX_TODOS:
        raise TodoError(f"清单最多 {MAX_TODOS} 条，收到 {len(result)} 条")

    active = [item.content for item in result if item.status == "in_progress"]
    if len(active) > MAX_IN_PROGRESS:
        raise TodoError(
            f"同时最多 {MAX_IN_PROGRESS} 条 in_progress，收到 {len(active)} 条："
            f"{'、'.join(active)}——一次做一件事"
        )
    return result


def fold_todos(events: Iterable[Event]) -> list[TodoItem] | None:
    """把事件流重放成"当前清单"；从未写过时返回 None（区别于空清单）。

    简单到只有一条规则：**最后一条 `todo/write` 赢**。因为每次写入都是完整
    快照，不需要按顺序重放每一条——这也是整表替换换来的好处。

    严格性：坏负载（缺字段、状态非法、重复条目）**当场报错**——
    坏日志宁可炸在读取处，也不要静默折出一张假清单。
    """
    latest: list[TodoItem] | None = None
    for event in events:
        if event.type != "todo/write":
            continue
        raw = event.data.get("todos")
        if not isinstance(raw, list):
            raise TodoError(f"todo/write 的 todos 必须是数组，收到 {type(raw).__name__}")
        latest = normalize(raw)          # 折叠期也校验：日志被改过要能被发现
    return latest


def summarize(todos: list[TodoItem] | None) -> str:
    """一行摘要（工具返回值与界面提示共用）。"""
    if todos is None:
        return "还没有任务清单。"
    if not todos:
        return "清单为空（全部清掉了）。"
    done = sum(1 for item in todos if item.status == "completed")
    active = next((item.content for item in todos if item.status == "in_progress"), None)
    tail = f"，正在做：{active}" if active is not None else ""
    return f"{done}/{len(todos)} 已完成{tail}"


__all__ = [
    "MAX_IN_PROGRESS",
    "MAX_TODOS",
    "TODO_STATUSES",
    "TodoError",
    "TodoItem",
    "TodoStatus",
    "fold_todos",
    "normalize",
    "summarize",
]
