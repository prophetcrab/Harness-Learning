"""todo 插件 —— 会话级任务清单（`_07`）：服务 + 模型工具 + `/todo` 人命令。

**和 goal（`_06`）的分工**（这条最要紧）：

    goal   完成条件：做完是什么样。跨很多轮才值得建；会驱动自动续跑。
    todo   执行步骤：接下来做哪几步。模型自己拆、边干边勾；**不驱动续跑**。

所以本插件**不续跑**——它是纯记录面。清单写进日志（`todo/write` 整表快照），
当前清单由 `fold_todos` 重放出来（最后一条赢）。

装了三样：

**① 服务面 `todos`**：按会话包一层 TodoStore（读 / 写整表 / 便利方法）。

**② 模型工具 `todo_write`**（一个就够，对齐 dsh）
    整表替换：每次调用发完整清单。工具描述里写清了三条纪律——
    先规划再动手、做完立刻勾（别攒着）、同时只留一条 `in_progress`。

**③ `/todo` 人命令**（REPL 与网页命令菜单共用）
    看一眼 / 加一条 / 勾掉 / 清空。**人不花模型的钱**：直接改清单，不走 turn，
    也不消耗轮次。

**清单为什么不驱动续跑**：todo 是"我想怎么干"，不是"你要求我干到什么时候"。
让清单也能续跑的话，模型随手列个十步计划就等于给自己签了一张十轮的支票——
目标（`_06`）已经承担了"持续推进"的职责，两套机制叠在一起会互相打架。
"""

from __future__ import annotations

from typing import Any

from components.todo import TodoError, TodoItem, TodoStore
from components.tools import define_tool
from kernel import Context
from plugins.events import TODO_CHANGED
from plugins.services import SESSIONS_SERVICE, TODOS_SERVICE, TOOLS_SERVICE

# ---------------------------------------------------------------------------
# 服务面
# ---------------------------------------------------------------------------


class TodoService:
    """按会话提供清单读写（每条会话一个 TodoStore，日志就是它的存储）。"""

    def __init__(self, ctx: Context, *, default_session: str = "default") -> None:
        self._ctx = ctx
        self._stores: dict[str, TodoStore] = {}
        #: 当前正在跑哪个会话（由 AgentService 在持锁时设置）——模型工具是全局
        #: 注册的，但清单要落到"这次对话"的日志里（`_06` 踩过这个坑）。
        self._current_session = default_session

    def set_current_session(self, session_id: str) -> None:
        self._current_session = session_id or self._current_session

    def session_of_record(self) -> str:
        return self._current_session

    def store(self, session_id: str | None = None) -> TodoStore:
        target = session_id or self._current_session
        cached = self._stores.get(target)
        if cached is not None:
            return cached
        sessions = self._ctx.get(SESSIONS_SERVICE)
        if sessions is None:
            raise TodoError("sessions 服务未就绪——清单需要会话日志才能存活")
        session, _, _ = sessions.open(target)

        def on_change(todos: list[TodoItem]) -> None:
            self._ctx.emit(
                TODO_CHANGED,
                session=target,
                count=len(todos),
                todos=[item.to_dict() for item in todos],
            )

        store = TodoStore(
            append=session.append,
            load_events=lambda: session.events,
            on_change=on_change,
        )
        self._stores[target] = store
        return store

    # ---- 读 ----
    def current(self, session_id: str | None = None):
        return self.store(session_id).current()

    def view(self, session_id: str | None = None) -> dict[str, Any]:
        return self.store(session_id).view()


# ---------------------------------------------------------------------------
# 模型工具
# ---------------------------------------------------------------------------

TODO_WRITE_DESCRIPTION = (
    "记录/更新本次工作的任务清单。**每次调用都要发完整清单——它整表替换旧的**"
    "（没有逐条修改这回事）。条目只有两样：content（一句祈使句）和 status"
    "（pending / in_progress / completed）。\n"
    "**什么时候该主动用**（不必等用户说「列个计划」）：\n"
    "  · 用户一次要求做**两件以上的事**（「先…然后…最后…」「帮我做三件事」"
    "「多个待处理项」）——**开工前先把它们写成清单**，别闷头一件件做；\n"
    "  · 活儿明显要跨多个步骤 / 多轮才能完成的；\n"
    "  · 中途发现自己漏了步骤、或计划有变——整表更新一次。\n"
    "用途与纪律：\n"
    "  1. 多步骤的活儿**先列计划再动手**：开工前把具体步骤写成清单；\n"
    "  2. 开始做哪条就把它标成 in_progress——**同时最多一条**（一次做一件事）；\n"
    "  3. **做完立刻勾掉**（标 completed），不要攒着一起勾；\n"
    "  4. **单步的小事别建清单**（如「算一下 2+3」）；清单里还剩活儿时至少有一条"
    " in_progress；\n"
    "  5. 全部做完之后清单可以留作记录，也可以传空数组清掉。\n"
    "约束（不满足会被拒绝）：内容不能为空、不能重复；同时最多一条 in_progress；"
    "最多 50 条。"
)


def _todo_tools(service: TodoService):
    """构造 `todo_write` 的实现（会话现取，见 TodoService 的说明）。"""

    def todo_write(todos: Any = None, **_extra: Any) -> dict[str, Any]:
        """整表写清单。

        两种参数形态都容忍（实测踩到过）：
        - 正常：`{"todos": [{"content": "...", "status": "..."}]}`
        - 模型多发一层：`{"todos": {"todos": [...]}}`——摊平，别丢东西。

        **`todos` 缺失时返回错误，而不是当成"清空"**：清空清单是个有后果的
        操作，不能因为模型漏了个参数就悄悄把用户的清单抹掉（实测：模型第一次
        调用漏了包装，清单被静默清空，日志里只剩一条空 todo/write）。
        真的要清空，必须显式传 `[]`。
        """
        payload = todos
        if isinstance(payload, dict):                    # 摊平多包的一层
            payload = payload.get("todos", payload)
        if payload is None:
            return {
                "error": "缺少 todos 参数。要写清单请传完整数组（可传 [] 清空）；"
                         "只改某一条不行——本工具整表替换。"
            }
        if not isinstance(payload, list):
            return {"error": f"todos 必须是数组，收到 {type(payload).__name__}"}
        session = service.session_of_record()
        try:
            written = service.store(session).write(payload)
        except TodoError as exc:
            return {"error": str(exc)}
        return {"ok": True, **service.view(session), "written": len(written)}

    return todo_write


# ---------------------------------------------------------------------------
# 插件本体
# ---------------------------------------------------------------------------


class TodoPlugin:
    """提供 `todos` 服务 + `todo_write` 工具（inject tools / sessions）。"""

    name = "todo"
    inject = (TOOLS_SERVICE, SESSIONS_SERVICE)

    def __init__(
        self,
        *,
        default_session: str = "default",
        register_tools: bool = True,
    ) -> None:
        self._default_session = default_session
        self._register_tools = register_tools

    def setup(self, ctx: Context):
        service = TodoService(ctx, default_session=self._default_session)
        ctx.provide(TODOS_SERVICE, service)

        if not self._register_tools:
            return None
        registry = ctx.require(TOOLS_SERVICE)
        registry.register(define_tool("todo_write", TODO_WRITE_DESCRIPTION), _todo_tools(service))

        def dispose() -> None:
            registry.unregister("todo_write")

        return dispose


def todo_plugin(
    *,
    default_session: str = "default",
    register_tools: bool = True,
) -> TodoPlugin:
    """工厂：`register_tools=False` 只提供服务（测试/极简场景）。"""
    return TodoPlugin(default_session=default_session, register_tools=register_tools)


__all__ = [
    "TODO_WRITE_DESCRIPTION",
    "TodoPlugin",
    "TodoService",
    "todo_plugin",
]
