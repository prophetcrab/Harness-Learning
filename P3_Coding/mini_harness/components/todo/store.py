"""todo 服务构件 —— 把清单写进会话日志（构件层；不认识插件）。

与 goal 的 store 同一个套路，但简单得多：**没有 CAS、没有阶段许可**——
整表替换之下，"改哪一条"这个问题不存在，每次调用都是"这是新清单"。

约束校验全在 `domain.normalize`（唯一入口）：校验不过就不写，
日志一个字节都不动——坏事件会毒死之后每一次重放。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from components.session.events import Event
from components.todo.domain import TodoItem, fold_todos, normalize, summarize


class TodoStore:
    """会话日志之上的清单操作面。

    对上层只暴露两个动作：**读**（当前清单）与**写整表**。
    """

    def __init__(
        self,
        *,
        append: Callable[[str, dict[str, Any]], Event],
        load_events: Callable[[], Iterable[Event]],
        on_change: Callable[[list[TodoItem]], None] | None = None,
    ) -> None:
        self._append = append
        self._load_events = load_events
        self._on_change = on_change

    # ---- 读 ----

    def current(self) -> list[TodoItem] | None:
        """当前清单（由日志现折；从未写过时 None）。"""
        return fold_todos(self._load_events())

    def view(self) -> dict[str, Any]:
        """给模型/界面看的读视图（含一行摘要，省得调用方自己数）。"""
        todos = self.current()
        return {
            "todos": [item.to_dict() for item in todos] if todos is not None else None,
            "summary": summarize(todos),
        }

    # ---- 写（整表替换） ----

    def write(self, items: Iterable[TodoItem | dict[str, Any]]) -> list[TodoItem]:
        """用一张新清单**替换**旧的；返回规整后的清单。

        空列表是合法的——含义是"清空清单"（事情做完或计划作废），
        与"从未写过"（None）不是一回事。
        """
        todos = normalize(items)
        self._append("todo/write", {"todos": [item.to_dict() for item in todos]})
        if self._on_change is not None:
            self._on_change(todos)
        return todos

    # ---- 便利方法（都只是"读-改-写"，不引入新的日志语义） ----
    #
    # 这些方法存在的唯一理由是"模型手里没有完整清单时的常见操作"；
    # 它们最终都汇到 write()，所以日志里永远只有整表快照这一种事件。

    def add(self, content: str) -> list[TodoItem]:
        """在末尾追加一条（pending）。"""
        todos = list(self.current() or [])
        todos.append(TodoItem(content=content.strip()))
        return self.write(todos)

    def set_status(self, content: str, status: str) -> list[TodoItem]:
        """改某一条的状态（按内容匹配——没有 id，内容就是身份）。"""
        todos = list(self.current() or [])
        for index, item in enumerate(todos):
            if item.content == content:
                todos[index] = TodoItem(content=item.content, status=status)  # type: ignore[arg-type]
                return self.write(todos)
        raise ValueError(f"清单里没有这一条：{content}")

    def clear(self) -> list[TodoItem]:
        """清空清单（写一条空的整表快照，而不是删历史）。"""
        return self.write([])


__all__ = ["TodoStore"]
