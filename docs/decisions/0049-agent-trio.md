# 0049 Agent 三件套：agent / agent-loop / agent-admin

- 日期：2026-10-16
- 阶段：P4_Coding（M2 收尾）
- 状态：已采纳

## 背景

用户给定的结构（对齐 dsh 的 `dsh-agent` + `dsh-agent-loop` 分工）：

> agent-admin —— base，管理不同的 agent 模式；必要依赖：agent、agent-loop
> agent：Agent 句柄与注册表：create/resume/get/list/roots、followup/steer/inject/
> cancel/whenIdle、agent/* 事件词汇
> agent-loop：默认循环：模型请求、流式响应、工具执行、持久历史

## 决策

### 零、分类：接缝归 base，实现归 capability（2026-10-16 修正）

初版把三个都标成 base。按 0041 的判据重新审视后改成：

| 插件 | 角色 | 理由 |
|---|---|---|
| `agent` | **base** | 接缝本身：注册表 + 句柄契约；换它等于改所有调用方的契约 |
| `agent-loop` | **capability** | 一种循环实现；换一个"一步收尾"的驱动器产品照常起 |
| `agent-admin` | **capability** | 模式预设是管理便利；没有它 `agent.create()` 照样能用 |

判据不是"卸掉产品会不会死"（卸掉 `llm-provider` 产品一样会死，但它是
capability），而是**"这是接缝还是填接缝的实现"**。"必须有实现"由装配策略
保证：`agent.requires = ("agent-loop",)`、`llm.requires = ("llm-provider",)`。

与 LLM 组完全同构：

    llm      base         路由 / 接缝
    agent    base         注册表 / 接缝
    llm-provider  capability   一种模型实现
    agent-loop    capability   一种循环实现

物理位置也随之调整：`plugins/capability/agent/loop.py`、`agent/admin.py`。

### 一、三层分工

| 插件 | 提供 | 职责 | 依赖 |
|---|---|---|---|
| `agent` | `agent` 服务 | **句柄与注册表**：谁在跑、怎么发消息、怎么停 | 无（驱动器惰性） |
| `agent-loop` | 驱动器（注册工厂） | **一轮怎么跑**：模型 → 工具 → 重复、事件落日志 | agent / llm / sessions / prompt / tool-admin（workspace 可选） |
| `agent-admin` | `agent-admin` 服务 | **模式**：预设几套配置，按名字创建 / 停止 / 列举 | agent（插件级：agent + agent-loop） |

**agent 包不创建模型请求**：没有驱动器注册工厂之前，`create()` 抛
`AgentNotAvailable`。换一个"一步就收尾"的假驱动器，agent 包与日志照常工作
（有测试 `test_agent_works_with_a_different_driver`）——这是 0040 差距 1
的可测形式。

### 二、AgentHandle 的五个输入语义

    followup(text)   排队下一轮；空闲时同步跑完并返回 TurnResult
    steer(text)      中途引导：下一个 step 边界生效，不新开一轮
    inject(text)     注入上下文，不唤醒驱动器（留到下一轮）
    cancel()         协作式中止（step 边界 / 流分块之间检查）
    when_idle()      等所有排队输入跑完

steer / inject 都落成 `user/message` 事件（投影成一条 user 消息），区别只在
`source` 字段与是否唤醒。**这就是"不新开一轮但让模型看到这句话"的实现**。

### 三、一轮的固定流程与事件

    turn/start
      └─ step N
           渲染提示词（指纹变了才写 system/message）
           agent/request（waterfall，可改写请求）
           llm.stream（工具表来自 tool-admin）
           assistant（文本 + tool_calls）
           tool/call → tool-admin 管线 → tool/result
           没有工具调用 → turn/end(done)
    turn/end(status, steps, final_text)

事件双写：`sessions.append`（持久真相）+ `ctx.emit("agent/*")`（观察与拦截）。
两者顺序固定：**先落日志，再广播**——观察者看到的永远是已经记下来的事实。

### 四、安全上限（实测教训）

反向验证时把 `max_steps` 去掉，假 provider 一直要求调工具，**测试直接挂死**。
这说明"不限步数"不能是默认值。所以：

- `AgentLoopPlugin.SAFETY_MAX_STEPS = 64`：没显式配置时的安全上限；
- 真要"不限"必须显式写 `default_max_steps: null`；
- 触顶以 `TurnResult.status = "max_steps"` 收尾（结构化，不抛异常）。

真实产品里模型很少连续调 64 次工具；这个数只用来兜住"模型陷进循环"。

### 五、模式（agent-admin）

| 内置模式 | 配置 |
|---|---|
| `default` | 不额外限制（模型走 llm 的装配默认） |
| `quick` | `max_steps=3` |
| `careful` | `max_steps=30` + `token_budget=100_000` |

模式可以有 `character`（角色名）：创建时自动 `character-admin.set(session, 角色)`。
装配单可整段覆盖内置模式、追加新模式、指定默认模式。

`create(mode=..., **overrides)` 的合并顺序：**模式 → 显式覆盖**。覆盖只作用于
这一次创建，不改模式本身。

### 六、不做（有触发条件再说）

- **并行工具调用**：现在串行执行；dsh 的 `maxParallelToolCalls` 等有真实
  性能需求再做；
- **父子 agent / 子代理**：`parent` 字段已在数据结构里，但没有创建子 agent
  的路径；
- **`when_idle` 的完整语义**：现在是句柄级空闲，没有"子 agent 也停稳"；
- **声明式常驻 agent**（配置里写 `agents:` 自动启动）：属于产品装配层，
  M4 的启动器做。

## 后果

- 一个真 turn 现在能跑通：demo 里"算 2+3"走完 模型 → calc → 回灌 → 回答，
  11 条事件落日志，投影出 4 条消息；
- `agent-loop` 是**默认驱动器**，不是唯一驱动器——注册任一实现
  `create/resume` 的工厂即可替换；
- 日志是唯一的会话真相：句柄的 `followup` 返回 `TurnResult`，但历史要从
  `sessions.messages()` 读，两者不会漂移（同一个投影）。

## 相关

- [0044](0044-llm-router-and-model-selection.md)（模型选择是数据）、
  [0047](0047-sessions-workspace-project.md)（事件与投影）、
  [0048](0048-tools-and-fs-policies.md)（工具管线）、
  [0040](0040-p3-wrap-up-vs-dsh.md)（差距 1：循环插件化）；
- dsh 对照：`dsh-agent`（句柄与注册表）/ `dsh-agent-loop`（默认驱动器）。
