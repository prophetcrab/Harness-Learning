# 记忆插件定义（dsh-task-memory）

> 按「定义 / 画像 / 投影」模板（外加定稿模板的第四段「代价与边界」）填写，内容对应当前已安装的 `dsh-task-memory` 0.2.0。

## 1. 定义

**这个插件是做什么的？**

给当前 DSH profile 增加一份跨会话、跨重启的「任务 + 长期事实」记忆：

- 任务带遗忘/再激活曲线，会自己回到「该重新想起」的集合；
- 事实按类别（偏好/人物/项目/决定/约束/事实）保存，带记忆强度；
- 每轮对话结束后由后台小模型自动判断并写入，定期合并去重；
- 向模型提供有界的记忆上下文与查询工具，向用户提供命令、每日简报、时点提醒和 `/brief` 视觉简报；
- 模型、用户、宿主看到的是同一份事实（同一 `state.json`）。

**我做这个插件要得到什么？**

1. **跨会话不失忆**：重启、新会话后模型仍知道待办、截止时间、承诺，以及用户的偏好、项目背景、已做决定和约束。
2. **低打扰的自动维护**：不依赖「模型想起来才记」，回合结束自动抽取；失败只记日志，绝不打断对话。
3. **可核对、可删除**：记忆有 id、来源会话、记忆强度、提及次数、闲置天数；用户能查（`/memories`）能删（`/memories forget`）。
4. **每天可执行的回顾**：09:00 文字简报 + `/brief` 会话内视觉简报（LLM 合成，失败回退本地模板）。
5. **可审计、可迁移**：一个账本文件 + 宿主 schedule 存储，不依赖外部服务。

## 2. 画像

### a. 事实：运行痕迹与持久化

**必须持久化：**

1. `$DSH_HOME/task-memory/state.json`（权威账本，临时文件 + rename 原子写）：
   - `tasks[]`：`id / title / status / priority / dueAt? / note? / createdAt / updatedAt / lastTouchedAt / lastShownAt? / touches / ignores / doneAt? / droppedAt? / reminderId? / reminderAt? / reminderSessionId? / sourceSessionId?`
   - `facts[]`：`id / kind / title / detail? / importance / mentions / lastSeenAt / createdAt / updatedAt / sourceSessionId?`
   - `digest`：`enabled / scheduleId / sessionId / time / timeZone / promptVersion / updatedAt`（每日简报绑定）
   - `consolidation.lastAt`：上次事实整理时间
   - `report`：当日视觉简报缓存（`dateKey / source / generatedAt / headline / insight / focus / facts / sections…`）；可丢弃重建，不属于权威记忆
   - `version`：文档版本
2. `$DSH_HOME/storages/schedule.json`（宿主 schedule 服务所有）：每天 09:00 简报记录 + 每条任务的一次性提醒记录；插件只持有 `scheduleId`，不直接写该文件。
3. 文件系统痕迹：损坏文档备份 `state.json.corrupt-<时间戳>.json`；写盘临时文件（正常完成后消失）。

**派生、不落盘：**

- 任务的记忆强度（由 `touches / ignores / lastTouchedAt` 计算）、事实的记忆强度（由 `importance / mentions / lastSeenAt` 计算）、闲置天数、排序分数。落盘的是输入量，强度是投影。

**日志痕迹：**

- `info`：抽取写入数量、整理合并/删除数量；`warn`：抽取/整理失败、简报回退、digest 绑定失败、状态损坏/条目被跳过。除标识和计数外不额外打印记忆内容。

**会话痕迹：**

- 工具调用（`task_remember / task_list / task_update / task_brief / memory_remember / memory_list / memory_extract`）与命令（`/tasks`、`/memories`、`/brief`）作为既有 session 事件被记录；
- 自动抽取在对话之外运行，**不新增 session 事件类型、不写对话**；
- 系统提示小节由 store 派生，随请求装配写入日志，因此模型可见内容可重建。

**不持久化/不拥有：**

- 对话原文（只保留结构化记忆与界内片段）、抽取计时器与缓冲（进程内内存）、UI 卡片状态与渲染状态。

### b. 行为：挂已有流程，还是新增能力

**两者都有，且只走文档化扩展点：**

- **挂在已有流程上**：`session/event`（`user/message`、`turn/end`）后自动抽取；`agent/assistant-stream` 累积助手文本；`systemPrompt.section()` 注入记忆小节；宿主 `schedule` 服务承载每日简报与提醒绑定；会话事件驱动 digest 重绑；浏览器半用 `conversation.chat.commandview` 挂聊天命令渲染点。
- **主动新增入口**：7 个工具、3 个命令、`/brief` 视觉简报卡片、每日 09:00 简报回合、一次性时点提醒、回环报告路由（`GET /task-memory/report`、`POST …/refresh`）。

**触发方式：** 回合结束（事件，1.5s 防抖）、每日定时、用户命令/工具、插件加载（digest 对账）、读取报告（懒生成）。

**不触发条件：** 无用户来源消息的回合（schedule 投递、subagent）、短于 30 字的回合、`autoExtract=false`、没有 `schedule` 服务的 profile（无简报/提醒）、非 Web（无视觉卡片，`/brief` 回退文本）。

**边界：** 不修改 agent-loop，不新增 session 事件类型，不新增可复用服务（纯消费者）。

### c. 能力：为完成什么任务而生

- **记**：从对话中自动识别「要做的事」和「值得长期保留的事实」，低置信宁可不记。
- **忆**：跨会话把任务/事实按有界预算注入模型上下文，并提供查询工具。
- **维**：任务遗忘/再激活、事实记忆强度、事实整理（合并/去重/删除）、用户手动完成/推迟/放弃/删除。
- **提**：时点提醒、每日简报。
- **呈**：文字简报、会话内视觉 `/brief`（LLM 合成 + 本地回退）。
- **非目标**：通用 RAG/知识库；不存对话原文；不做多进程共享存储；模型没有直接删除事实的常规工具（删除走用户命令、抽取 `forget` 或整理）。

## 3. 投影

### 模型看到了什么

- 系统提示小节 `## 跨会话长期记忆`：写入规则 + 当前任务（按紧迫度，≤12 条 / 2400 字符，含 id）+ 长期事实（按记忆强度，≤8 条，含 id 与强度百分比）。
- 7 个工具的定义与结果；`task_brief` 返回确定性文本简报；`memory_list` 返回带强度的结构化事实。
- 每日 09:00 digest 回合：宿主投递的固定 prompt（要求调用 `task_brief` 并原样输出）。
- `/brief` 命令的文本结果（`reportText`）作为会话命令输出；视觉卡片本身不进模型上下文。
- **看不到**：自动抽取/整理的后台请求（脱离会话、无会话身份）、`state.json` 内部细节、报告缓存、UI 状态。

### 系统（宿主）看到了什么

- 插件纤维与注册：tools / commands / systemPrompt / schedule 绑定 / HTTP 报告路由 / client `commandview`。
- `state.json` 与宿主 `schedule.json` 中的记录；运行日志（info/warn）。
- 报告路由（仅回环，拒绝跨站）与其 JSON 响应。
- **可检查证据**：state 字段、schedule 记录 id、日志行（`extraction stored` / `consolidation` / `fell back`）、路由响应、当前工具列表。

### 用户看到了什么

- 模型回答中的记忆效果；工具调用记录。
- `/tasks`（未完成任务 + 编号 + 记忆强度 + 简报开关）、`/tasks brief`、`/tasks report`、`/tasks add|done|later|drop`、`/tasks digest on|off`。
- `/memories`（事实 + ★重要度 + 记忆强度% + 提及次数 + 闲置天数）、`/memories forget <id>`。
- `/brief` 会话内视觉简报（统计卡、今日焦点、记忆洞察、风险、一句话、任务全景，带刷新按钮）；每天 09:00 的文字简报；时点提醒消息。
- 没有侧边栏入口：视觉简报只出现在会话里。

### 失败形态（三视角同时看）

| 失败 | 模型 | 宿主 | 用户 |
|---|---|---|---|
| 抽取/整理失败 | 无感 | 日志 warn，记录原因 | 无感 |
| 报告 LLM 失败/超时/关闭 | `/brief` 文本仍可用 | 日志 warn，缓存标记 `source=fallback` | 卡片保留内容并标注「本地回退」 |
| `state.json` 损坏 | 记忆清空但可继续用 | 先备份 `.corrupt-*` 再空账本启动 + warn | 表现为记忆清空（可查日志） |
| 无 `schedule` | 无简报回合 | digest 绑定不执行 | `/tasks digest` 报错，无提醒/简报 |
| 非 Web / 无 `webServer` | 无变化 | 不注册报告路由 | `/brief` 返回文本版 |
| 单进程并发 | 无变化 | 无跨进程锁（与 DSH JSON 存储一致） | 同左 |

## 4. 代价与边界（补充，沿用定稿模板第四段）

- **每次运行成本**：每完成回合 1 次小模型抽取请求（可关）；事实整理每 20 条新事实或每天 1 次；视觉简报每天 1 次 + 每次手动刷新；digest 回合是一次对话模型回合（schedule 语义）。
- **失败表现**：见上表，全部「只降级、不打断」。
- **卸载残留**：`state.json` 保留；宿主 `schedule` 中的提醒记录不会随插件卸载自动删除（先 `/tasks digest off` 或手动清理）；client 卡片随模块卸载消失；工具/命令/系统提示小节随纤维卸载撤销。
- **降级形态**：`autoExtract=false` → 仅显式工具；`reportEnabled=false` → 本地模板；无 `schedule` → 无简报/提醒；无 `webServer` → 文本简报；辅助模型不可用 → 抽取跳过、简报回退模板。
