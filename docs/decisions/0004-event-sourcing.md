# 0004 会话日志：日志是唯一真相，消息由投影派生

- 日期：2026-10-07
- 练习：P1_Coding/_04_Session_Log
- 状态：已采纳

## 背景

_02 把循环的产出做成了 `TurnResult → Step → ToolResult` 轨迹，但那份轨迹**只活在
内存里**：进程一退就全没了。这带来三个做不到的事：

1. **无法恢复**。对话中途退出（不管是正常退出还是 Ctrl+C / kill -9），历史蒸发。
2. **无法审查**。想事后看"第 3 步模型看到了什么"，得在运行时打印；错过了就没了。
3. **真相分裂**。轨迹里一份历史、循环里一份历史、模型请求里一份历史——三者一致
   纯靠约定，没有任何机制保证。M3 的核心断言"重放日志得到的历史 == 在线产生的
   历史"在这种结构下根本无从谈起。

学习计划 M3 要求把周期性的"内存轨迹"换成 **append-only 事件日志 + 投影**：
模型历史不是被存储的东西，而是日志的一个视图。这是全项目的心脏。

## 决策

建立事件溯源式的会话日志，分成"事实 / 视图 / 持久化 / 装配"四层：

- **事件模型**（`session/events.py`）：8 类事件（`session/start`、`turn/start|end`、
  `user/message`、`step/start|end`、`assistant/message`、`tool/result`）；Event 带
  单调 `seq` + `ts` + 负载；**写入前校验**（未知类型 / 缺必填字段直接 fail loud）。
- **投影**（`session/projection.py`）：`derive_messages(events) -> list[Message]`
  是**纯函数**。四类消息事件投影成 system/user/assistant/tool 消息，生命周期事件不产生
  消息。**消息永远由日志派生，绝不直接存**（铁律 #1/#2 的落地）。
- **日志本体**（`session/log.py`）：`Session` 只有 `append`（无 update/delete）；
  进日志深拷贝、读出来也深拷贝（frozen dataclass 管不住里面的 dict，靠拷贝）；
  seq 由日志自身维护。
- **持久化**（`session/store.py`）：`<root>/<id>/session.jsonl`，每条 append 后
  flush；`load()` 逐行解析并记录"最后一个完整合法行"的结束位置，**把其后残留的
  半截字节截掉**（kill -9 恢复）。
- **装配**（`runner.py`）：`open_session(id)` 对新建/已存在走**同一条路径**（装载 →
  投影 → 建循环）；`SessionRecorder` 把 AgentLoop 的 on_event 翻译成会话事件；
  `fork_session` 用重放前缀派生新会话。
- **循环侧最小改动**：`agent_loop` 加 `initial_messages` / `initial_turn`（resume 用）
  与事件负载增强（完整 tool_calls、call_id、step_end），使事件流足以重建日志。

## 备选与排除理由

1. **把 `TurnResult` 序列化后存起来当"历史"**。排除：那就是"存消息列表"的变体——
   存的是结果而非事实，一旦想改"给模型看什么"（裁剪/压缩）就得改存储格式，且丢失
   "为什么变成这样"的过程。事件日志存的是**决策序列**，视图可以随策略重算。
2. **用 SQLite 而不是 JSONL**。排除：JSONL 的"一行一事件、可读、可 diff、append 友好"
   更适合学习与调试；崩溃修复这个主题在 JSONL 上最直白（半行就是半行）。SQLite 的
   事务原子性会掩盖"自己动手修尾行"这个要练的点。
3. **每次 append 都 fsync**。排除：每条事件一次 fsync 代价高，本练习的可靠性目标
   是"已确认事件不丢"，flush（用户态缓冲落内核）已足够演示；fsync 策略是后续优化项。
4. **恢复做成单独分支**（`resume()` vs `open()`）。排除：那会让"恢复"成为特例，而
   事件溯源的美感正在于"打开即恢复"——新建会话不过是"日志为空"的恢复。同一条路径
   才能保证两条路不会行为分叉。
5. **fork 用写时复制 / 引用式**。排除：引用式需要"代际"概念（同一条日志被多个会话
   共享），复杂度远超本练习目标；拷贝式前缀已能表达"从某个 seq 分叉"的语义。

## 后果

- 约束：`Session` 只增不改；`derive_messages` 必须保持纯函数；事件写入前必过校验；
  循环侧新增的模型可见输入必须同步成事件（否则破坏 #1）。
- 收益：13 个测试全离线；同构断言通过（重放 == 在线）；崩溃尾行两种形态都能修复；
  resume / fork 有测试保证；system 提示进日志，恢复时不再重复注入。
- 债务：无日志代际/压缩，日志无上限；无写锁（单写者假设）；fork 是拷贝式；
  多练习同名包需 `conftest.py` 驱逐（见 `_04` README 的工程备注）。
- 后续：`_05_Mini_Harness` 把前四者组装成 CLI；M4 起把 system 提示也从"一条
  session/start"升级成可装配、可追溯的 section 注册表；M7 的 `session.follow`
  = 重放（日志）+ 订阅（实时），本练习的投影正是"重放"那一半。
