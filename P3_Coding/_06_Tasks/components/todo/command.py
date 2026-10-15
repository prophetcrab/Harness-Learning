"""todo 命令 —— `/todo` 的人机接口（不花模型的钱）。

与 `/goal` 同一个套路（`components/goal/command.py`）：解析 + 排版是纯逻辑，
收在构件层，网页与 REPL 共用一份，避免两个入口各写一遍导致漂移。

用法：

    /todo                    看清单（带序号，方便勾）
    /todo <内容>              追加一条（**最常用，不用打 add**）
    /todo add <内容>          同上
    /todo done <序号或内容>    勾掉一条
    /todo doing <序号或内容>   标记"正在做"
    /todo todo <序号或内容>    退回"待办"
    /todo rm <序号或内容>      删掉一条
    /todo clear              清空清单
    /todo run                **把清单交给模型开始干**（这条会花模型的钱——它会
                             逐条执行，每完成一条就勾掉）

**序号是给人用的速记**：清单短、人一眼能看到第几条，敲 `3` 比抄一整句快得多。
模型侧没有这套东西——它必须发整表（`todo_write`），因为模型不该维护索引意识。

**`/todo` 是记事本，不是开工按钮**（这条踩过）：它只读写清单，**永远不调模型**。
要模型干活有两条路：

    /todo run                 把当前清单交给模型执行（花模型的钱）
    直接把需求发成消息          模型会自己拆清单并执行（推荐用于"给需求"的场景）

于是 `/todo 先帮我做个游戏再写文档` 这种**整句需求**是被误用的入口：它只会把
整句话记成一条任务。`_looks_like_request()` 会在这种输入上补一句提示。

返回值是**给人读的文本**，永不抛异常：打错要看到用法，而不是 traceback。
"""

from __future__ import annotations

from dataclasses import dataclass

from components.session.events import Event  # noqa: F401  （类型提示用，保持导入风格一致）
from components.todo.domain import TodoError, TodoItem, summarize
from components.todo.store import TodoStore

USAGE = (
    "用法：/todo [<内容> | add <内容> | done <序号|内容> | doing <序号|内容> | "
    "todo <序号|内容> | rm <序号|内容> | clear]\n"
    "  不带参数 = 看清单；直接写内容 = 追加一条。"
)

#: 子命令别名 → 规范动作。
_ALIASES = {
    "add": "add", "加": "add", "追加": "add",
    "done": "done", "完成": "done", "勾": "done", "好了": "done",
    "doing": "doing", "在做": "doing",
    "todo": "todo", "待办": "todo", "退回": "todo",
    "rm": "rm", "del": "rm", "删": "rm", "删除": "rm", "去掉": "rm",
    "clear": "clear", "清": "clear", "清空": "clear",
    "run": "run", "start": "run", "go": "run",
    "开始": "run", "开干": "run", "执行": "run", "干活": "run",
}

_STATUS_OF_ACTION = {"done": "completed", "doing": "in_progress", "todo": "pending"}

#: 状态 → 给人看的勾选标记。
_MARKS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}


#: `/todo run` 的别名（把清单交给模型执行）。
_RUN_ALIASES = {"run", "start", "go", "开始", "开干", "执行", "干活", "做"}


def is_run_request(line: str) -> bool:
    """这一条 `/todo` 是不是"开始执行清单"？"""
    args = line.strip().split()
    return bool(args) and args[0].lower() in _RUN_ALIASES


RUN_HEADER = "按下面的任务清单开始工作"


def build_run_prompt(todos: list[TodoItem]) -> str:
    """把清单包成一条消息，交给模型执行（`/todo run` 用）。

    刻意写成**普通的用户消息**而不是什么特殊协议：模型照常走既有的循环
    （读文件、写代码、勾清单），日志里也是一条正常的 `user/message`。
    这就是"启动"的全部——不需要新机制。
    """
    lines = [f"{RUN_HEADER}："]
    for index, item in enumerate(todos, 1):
        lines.append(f"  {index}. {item.content}")
    lines.append(
        "\n要求：逐条做，**每完成一条就用 todo_write 把整表更新一次**"
        "（该条标 completed，同时把正在做的那条标 in_progress，至多一条）；"
        "全部做完后给我一句总结。中途发现计划要改就直接改清单。"
    )
    return "\n".join(lines)


def _looks_like_request(text: str) -> bool:
    """这句"任务内容"看起来是不是一整句需求（而不是一个清单条目）？

    判据取得很保守（宁可漏判，也不要把正常条目误报）：出现的分句连接词
    或长度超标，才认为像需求。用来在误用时补一句提示。
    """
    if len(text) >= 30:
        return True
    return any(marker in text for marker in ("，再", "，然后", "并且", "，以及", "，最后"))


@dataclass(frozen=True)
class TodoCommandResult:
    """一次 `/todo` 的结果。

    `run_prompt` 非空表示"这条命令要求把清单交给模型执行"——构件层只负责
    拼出那条消息（不认识 agent），由入口决定怎么发（网页/REPL 各自调 agent.ask）。
    """

    text: str
    changed: bool = False
    run_prompt: str | None = None


def format_todos(todos: list[TodoItem] | None) -> str:
    """把清单排成人看的一小段（**带序号**，因为下一步就要用它勾）。"""
    if todos is None:
        return "还没有任务清单。用 `/todo <内容>` 加一条。"
    if not todos:
        return "清单是空的。"
    lines = [f"任务清单（{summarize(todos)}）："]
    for index, item in enumerate(todos, 1):
        lines.append(f"  {index}. {_MARKS[item.status]} {item.content}")
    return "\n".join(lines)


def _resolve(todos: list[TodoItem], target: str) -> TodoItem:
    """按序号或内容定位一条（序号优先——1 起算，与人看到的一致）。"""
    if target.isdigit():
        index = int(target)
        if not 1 <= index <= len(todos):
            raise TodoError(f"序号超出范围：{target}（清单有 {len(todos)} 条）")
        return todos[index - 1]
    for item in todos:
        if item.content == target:
            return item
    # 内容没完全命中时给个"包含"的兜底，省得人工抄长句
    matches = [item for item in todos if target in item.content]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise TodoError(f"「{target}」匹配到多条，请用序号：{'、'.join(m.content for m in matches)}")
    raise TodoError(f"清单里没有这一条：{target}")


def run_todo_command(store: TodoStore, line: str) -> TodoCommandResult:
    """执行一条 `/todo ...`；`line` 是去掉 `/todo` 之后的剩余文本。

    与 `/goal` 同一条规矩：先看第一个词是不是子命令，是就执行，
    不是就把整行当内容——于是"想说任务就直接说"。
    """
    args = [piece for piece in line.strip().split() if piece]
    if not args:
        return TodoCommandResult(format_todos(store.current()))

    raw_action, rest = args[0], args[1:]
    action = _ALIASES.get(raw_action.lower())

    try:
        if action is None:
            # 不是子命令 → 整行当新条目（`/todo 跑一遍测试` 直接追加）
            content = " ".join(args)
            store.add(content)
            # **误用提示**：`/todo` 只是记事本，不会让模型开工。用户很容易把
            # 一整句需求丢进来（"先做 A 再写 B"），结果只记下一条、什么也不发生。
            # 这种输入上补一句人话，指明正确入口。
            hint = "要模型开始做，用 `/todo run`（把清单交给它执行）。"
            if _looks_like_request(content):
                hint = (
                    "⚠ 这看起来是**一整句需求**，不是清单条目——`/todo` 只记条目、"
                    "不会开工。\n要用模型拆分并执行，请**把这句话直接当消息发出去**"
                    "（它会自己拆清单再逐条做）；刚才这条想删就 `/todo rm 1`。"
                )
            return TodoCommandResult(
                "已加入：\n" + format_todos(store.current()) + "\n" + hint, changed=True
            )
        if action == "add":
            content = " ".join(rest).strip()
            if not content:
                return TodoCommandResult(f"add 需要内容。\n{USAGE}")
            store.add(content)
            return TodoCommandResult("已加入：\n" + format_todos(store.current()), changed=True)
        if action == "clear":
            store.clear()
            return TodoCommandResult("清单已清空。", changed=True)
        if action == "run":
            # `run` 不在这里执行：它需要"把消息交给模型"这一步，而那是入口的职责
            # （构件层不认识 agent）。入口看到 run_prompt 后自己调 `agent.ask(...)`。
            todos = store.current() or []
            if not todos:
                return TodoCommandResult(
                    "清单是空的，没什么可执行的。先用 `/todo <内容>` 加几条。"
                )
            return TodoCommandResult("（交给模型执行）", run_prompt=build_run_prompt(todos))

        # done / doing / todo / rm 都要先定位一条
        todos = store.current() or []
        if not todos:
            return TodoCommandResult("清单是空的，没什么可改的。")
        target = " ".join(rest).strip()
        if not target:
            return TodoCommandResult(f"{raw_action} 需要序号或内容。\n{USAGE}")
        item = _resolve(todos, target)

        if action == "rm":
            store.write([t for t in todos if t.content != item.content])
            return TodoCommandResult(
                f"已删掉「{item.content}」：\n" + format_todos(store.current()), changed=True
            )
        store.set_status(item.content, _STATUS_OF_ACTION[action])
        verb = {"done": "已完成", "doing": "正在做", "todo": "退回待办"}[action]
        return TodoCommandResult(
            f"「{item.content}」{verb}：\n" + format_todos(store.current()), changed=True
        )
    except (TodoError, ValueError) as exc:
        return TodoCommandResult(f"操作被拒绝：{exc}")


__all__ = [
    "USAGE",
    "TodoCommandResult",
    "build_run_prompt",
    "format_todos",
    "is_run_request",
    "run_todo_command",
]
