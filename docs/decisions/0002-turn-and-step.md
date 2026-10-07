# 0002 turn 与 step：循环抽成类，轨迹成为一等返回值

- 日期：2026-10-07
- 练习：P1_Coding/_02_Agent_Loop
- 状态：已采纳

## 背景

_01 的 `run_tool_loop` 是一个函数，返回 `LoopResult(status, messages, steps, final_text)`
四个扁平字段。用它已经能跑通"模型 ⇄ 工具"闭环，但三件事做不了：

1. **没有 turn 边界**。`steps` 只是"模型调了几次"的计数，"用户一次提问"与
   "模型一次往返"这两个层次被混在一起，无法表达"一个 turn 里多个 step"。
2. **轨迹无处安放**。循环过程只能靠 `on_event` 打印，程序拿不到结构化的
   "第几步发了什么请求、模型回了什么、工具执行了什么"——这恰恰是 M3 事件
   溯源要投影的原材料。
3. **无法复用、无法取消**。函数无状态，多轮对话的历史要由调用方自己传；也没有
   外部打断的通道（Ctrl+C 只能粗暴地炸掉整个进程）。

学习计划 M1 的验收明确要求"超出 step 预算时返回结构化终止""区分 turn 与 step"，
dsh 的 `agent-loop` 也把 turn（inbox 驱动的输入队列单元）与 step（turn 内部的
一次模型往返）作为两级概念——继续用函数就会把这些结构压平。

## 决策

把循环抽成 `AgentLoop` 类，引入三层轨迹词汇，并把取消做成检查点：

- **词汇**（`agent_loop/trace.py`）：`TurnResult → Step → ToolResult` 三层
  dataclass。`Step.index` 是"本 turn 内的第几步"，`Step.request` 保存发给模型的
  完整消息快照——turn 与 step 的关系、以及"模型看到了什么"都因此落成结构。
- **循环**（`agent_loop/loop.py`）：`run(user_text, tools) -> TurnResult` 处理一个
  turn；历史由循环持有（system 提示只在构造时注入一次，多 turn 累积）。事件回调
  从 _01 的 `on_event(kind, payload)` 沿用，只是事件集合扩展到 turn 边界
  （`turn_start` / `turn_end`）。
- **取消**：`cancel()`（粘性标志）+ `should_stop` 谓词，在每次 step（模型调用）
  之前检查，命中即返回 `status="cancelled"`。

依旧只依赖 `LLMProvider` 协议，`loop.py` 不 import 任何厂商 SDK。

## 备选与排除理由

1. **继续用函数，靠返回值 + 参数堆料**。排除：无状态函数无法持有跨 turn 历史
   （除非引入全局或让调用方手传），轨迹也没有自然的安放位置；这正是要解决的痛点。
2. **直接上 M3 事件溯源（append-only 日志 + 投影）**。排除：事件溯源的心智负担
   （append/校验/投影/持久化）会掩盖 turn/step 本身这个更基础的概念；先有"轨迹
   对象"，M3 再讨论如何把它投影成事件日志，顺序才顺。
3. **同步引入 dsh 的 inbox（异步输入队列）**。排除：inbox 解决的是"多个输入排队、
   边跑边收新输入"的问题，minimal loop 用同步 `run(user_text)` 已足够表达 turn 语义；
   队列化是后续主题，先不背这个复杂度。
4. **取消用异常（抛 `Cancelled`）实现**。排除：取消在这里是"正常收尾的一种方式"
   而非错误，用返回值表达（`status="cancelled"`）与 `done`/`max_steps` 并列，
   调用方不必写 try/except；也避免异常穿透到 provider 边界。

## 后果

- 约束：`loop.py` 不得 import 厂商 SDK；轨迹是 dataclass、构造后不改（为 M3 的
  append-only 铺垫）；取消是粘性的、只在 step 边界生效。
- 收益：13 个测试 0.03 秒全离线跑完；轨迹可事后断言（请求快照、工具结果都在）；
  多 turn 历史自然累积，为 `_05` 的 CLI 对话铺路。
- 债务：历史仍在内存、退出即忘（`_04`）；`complete()` 非流式（与 0001 同源）；
  工具级超时中断不在本练习（`_03` 的 execute 管线）。
- 后续：`_03_Tool_Pipeline` 在"执行工具"这一步插入 pre/execute/post 管线；
  `_04_Session_Log` 把 `TurnResult`/`Step` 投影成 append-only 事件并落盘。
