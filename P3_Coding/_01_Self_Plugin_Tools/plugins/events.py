"""events —— 内核事件与瀑布的名字（插件之间的协作点）。

分两类（**同名事件只属于一类**——签名不同，混用会立刻报错）：

**广播**（`ctx.emit`，只读通知；handler 用 `**payload` 接）
    llm/response    一次模型调用**之后**（payload: request, response, duration_ms）
    turn/start      一个 turn 开始（payload: session, turn, text）
    turn/end        一个 turn 结束（payload: session, turn, status, steps, final_text）
    session/opened  会话被打开（payload: session, resumed, repaired）
    session/event   会话**追加了一条事件**（payload: session, record）——每条一推，
                    RPC `session.follow` 的推送源（sessions 插件的 ObservableSession 发）
    plugin/loaded   插件被装载（payload: name, status）——自扩展工具的留痕
    plugin/unloaded 插件被卸载（payload: name）——自扩展工具的留痕
    approval/requested  一次人工审批被提交（payload: pending）——浏览器审批
    approval/resolved   人工审批有了结果（payload: id, tool, approved, note）
    loop/event      主循环的原始事件（payload: session, kind, payload）

**瀑布**（`ctx.waterfall`，可改写值；handler 用 `(value, **context)` 接，返回非 None 即替换）
    llm/request       发出前的模型请求（value: LLMRequest）——可以换模型、加工具
    prompt/variables  渲染系统提示词前的变量表（value: dict）——可以加/改时间等

"只想观察"的插件也能订阅瀑布：签名照写，返回 None 即可（不改值）。

用法示例（一个插件给提示词注入"固定时钟"）：

    class Clock:
        name = "clock"

        def setup(self, ctx):
            ctx.on(
                "prompt/variables",
                lambda value, **kw: {**value, "time": "2026-10-13T00:00:00+08:00"},
            )

事件名用 "域/动作" 的斜杠风格（与 dsh 的 `llm/stream`、`session/start` 一致）。
"""

# 广播
LLM_RESPONSE = "llm/response"
TURN_START = "turn/start"
TURN_END = "turn/end"
SESSION_OPENED = "session/opened"
SESSION_EVENT = "session/event"
PLUGIN_LOADED = "plugin/loaded"      # 自扩展工具：装载留痕（`_01`）
PLUGIN_UNLOADED = "plugin/unloaded"  # 自扩展工具：卸载留痕（`_01`）
APPROVAL_REQUESTED = "approval/requested"  # 浏览器审批：提交（`_01`）
APPROVAL_RESOLVED = "approval/resolved"    # 浏览器审批：有结果（`_01`）
LOOP_EVENT = "loop/event"  # 主循环的原始事件（kind + payload），观测用

# 瀑布
PROMPT_VARIABLES = "prompt/variables"
LLM_REQUEST = "llm/request"

__all__ = [
    "APPROVAL_REQUESTED",
    "APPROVAL_RESOLVED",
    "LLM_REQUEST",
    "LLM_RESPONSE",
    "LOOP_EVENT",
    "PLUGIN_LOADED",
    "PLUGIN_UNLOADED",
    "PROMPT_VARIABLES",
    "SESSION_EVENT",
    "SESSION_OPENED",
    "TURN_END",
    "TURN_START",
]
