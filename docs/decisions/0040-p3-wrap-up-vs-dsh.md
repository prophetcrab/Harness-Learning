# 0040 P3 收尾：交付清单、与 dsh 的三处差距、将来接着做的地方

- 日期：2026-10-16
- 阶段：P3_Coding（整阶段收尾）
- 状态：已采纳

## 为什么有这份记录

用户在 P3 收尾时问了一个很好的问题：**"这个项目里哪个部分不是插件？"**
顺着这个问题核对参考项目 dsh 的自我描述，发现三处"还没做到完全插件化"的地方。
用户决定**先收尾、暂不补**——所以这份记录的作用是：把"差距"变成**可查的清单**，
将来谁想继续推进"完全插件化"，从这里接着做，而不用重新推一遍。

## 一、P3 交付了什么（`_01`–`_06`）

| 阶段 | 机制 | 决策 |
|---|---|---|
| 基线 | 完全插件化（kernel 三层 + 服务/事件/effect/inject + 界面/RPC 都是插件） | 0020–0026 |
| `_01` | 自扩展工具（模型装卸白名单插件）+ 浏览器审批 + 从工作区安装插件 | — |
| `_02` | 流式输出 + 协作式取消 | — |
| `_03` | 用量账本与预算 | — |
| `_04` | 上下文仪表（容量 / 构成 / 缓存命中） | 0027 |
| `_05` | 上下文压缩（日志折叠声明 + 重放同构） | 0028 |
| `_05` 收尾（四轮） | 三处耦合修复 / 壳的两条扩展点 / 插件管理 / turn 执行器 | 0031–0035 |
| `_06` | **工具插件化**：工具面拆成底座 + 工具插件，可管理、可自装 | 0036–0038 |
| `_06` 收尾 | 端口让路 / 面板指纹 / 被截断日志的投影修剪 | 0039 |

门禁：`ruff` + 六个阶段目录 + 基线，逐目录 pytest 全绿（`_06` 423、`_05` 与基线各 351）。

## 二、核对的口径

dsh 的自我描述（`docs/architecture.md`）：

> Every part of the product is a plugin, including the model adapter, the tool
> registry, the session log, **and the agent loop itself**, so each is
> **replaceable from configuration**. **There is no privileged core to patch.**

**本包与之一致的四处**（都不是插件，但 dsh 里也不是）：

| 我们的 | dsh 里的对应物 | 为什么不是插件 |
|---|---|---|
| `kernel/`（context / plugin / loader） | `vendor/cordis`（vendored 源码） | **框架本身**——它不能是自己的插件 |
| `components/`（工具管线、日志、提示词…） | 纯库（如 `core/scope` 标注 "library, no key"） | 无生命周期、被多个插件共用 |
| `chat.py` / `run.bat` | `dsh` CLI / `apps/cli` | **启动器**：决定装哪些插件的那一层 |
| `webui/`（index.html / app.css / app.js） | `apps/web` 的静态资源 | 是 **webui 插件**端出来的资源；插件一卸就没了 |
| `tests/` `sessions/` | 测试与 `$DSH_HOME` 数据 | 不是产品代码 |

## 三、三处真实差距（**暂不补**）

| # | 差距 | 我们的现状 | dsh 的样子 |
|---|---|---|---|
| 1 | **主循环不是插件** | `components/agent/loop.py`；由 `plugins/agent.py` 直接 `AgentLoop(...)`（全仓唯一调用点） | `core/agent-loop` 是 "The default driver"，槽位 `ctx.agentLoop`——可整体替换 |
| 2 | **工具管线不属于 tools 插件** | 管线由 **agent 插件**构造；`tools` 插件只提供注册表 | `core/tools` 同时拥有 "The scoped tool registry **and guarded execution pipeline**" |
| 3 | **管线策略层没有插座** | `middlewares=` 只是 agent 插件的构造参数，**没有任何装配会传**；`DenyTools`/`RequireApproval`/`TruncateOutput` 在测试外无人使用；`_03` 的预算检查硬写在循环里（`loop.py:291`） | `guard/timeout-policy`、`guard/repeat-tool-reminder` 各是一个插件，"a composition can tune or remove them" |

第 3 条最值得记：**中间件机制存在但没有插座**——机制写了，没人能用，等于"看起来
插件化、实际策略只能写进循环"。它和 1/2 是同一件事的三个面：策略挂不到循环旁边，
就只能长在循环里。

**还有一处配套差距**（PLAN 早记过）：dsh 的 "replaceable **from configuration**"
是数据（profile / bundle / `cordis.yml` + patch）；我们的组合写在 `boot.py` /
`chat.py` 的参数里，是代码。配置分层 P2 做过，P3 刻意推迟到"在这个底座上重做"。

## 四、将来接着做的话，一个阶段三件事

1. **循环独立成插件**：提供 `agent-loop` 服务，agent 插件 `inject` 它——与 dsh 的
   `core/agent` + `core/agent-loop` 同形；换循环不用动 agent；
2. **管线搬进 tools 插件**：注册表与执行管线同一个 owner（对应 dsh 的 `core/tools`）；
3. **两个 guard 插件**：超时策略 + 预算（把 `_03` 的检查从循环里搬出来），
   顺手把 `DenyTools` / `TruncateOutput` 这些死中间件接上——**接不上就删掉**，
   不留没有插座的机制。

补齐之后可以说"完全插件化"与 dsh 同形；在此之前，准确的说法是
**"内核与能力都是插件，但循环与管线策略层还长在底座里"**。

## 相关

- 用户的问题："这个项目里哪个部分不是插件？"与"这不符合我们原本对于学习 dsh 的
  完全插件化的目标啊"——本记录是对这两问的结论；
- [P3_Coding/README.md](../../P3_Coding/README.md) 第 8 节（同内容的读者视图）；
- 决策 0020（P3 为什么先做完全插件化）；PLAN.md 的"P3 已收尾"标注。
