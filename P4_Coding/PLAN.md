# P4 阶段计划（实质化：插件底座 + 三类插件 → 一个可用产品）

**P4 与 P2/P3 的组织方式不同：不再是"阶段副本"。**

    P0–P2   预热 → 骨架 → 产品化（每阶段一个自包含练习目录）
    P3      完全插件化：`_01`–`_06` 每阶段一个副本 —— 学"一个机制怎么长出来"
    P4      ★ 实质化：一个项目（本目录）本身就是产品；里程碑只是内部进度

**状态：🏗️ M1 / M2 完成，M3 部分落地（2026-10-19）。** 全部从零重写、逐个小步走
（决策 [0043](../../docs/decisions/0043-p4-rewrite-from-scratch.md)）；**M1 完成**：
8 步全绿，另补了动态依赖 `ctx.inject`；**M2 完成**：LLM 能力组（`llm` 路由主插件 +
`llm-provider` 通用适配器 + `llm-retry` + `llm-tokens-usages`，决策
[0044](../../docs/decisions/0044-llm-router-and-model-selection.md) /
[0045](../../docs/decisions/0045-llm-capability-group.md)，含插件级依赖 `requires`、
图片输入、流式安全的重试、统计只读）；提示词组（`prompt` + `character-admin` +
`system-prompt-admin`，决策 [0046](../../docs/decisions/0046-prompt-plugins.md)）；
会话与项目组（`sessions` + `session-store-jsonl` + `workspace` + `workspace-admin` +
`project`，决策 [0047](../../docs/decisions/0047-sessions-workspace-project.md)）；
工具面（`tools-admin` + `fs` / `proc` 约定 + 沙箱与读后写策略 + 六个工具插件，
决策 [0048](../../docs/decisions/0048-tools-and-fs-policies.md)）；LLM 层补齐
**工具调用**；**Agent 三件套**完成（`agent` 句柄与注册表 + `agent-loop` 默认驱动器 +
`agent-admin` 模式管理，决策 [0049](../../docs/decisions/0049-agent-trio.md)）——
**一个真 turn 已跑通**；**最小应用**完成（launcher + 装配单 → 离线 / 真实模型两种
产品形态，决策 [0050](../../docs/decisions/0050-minimal-app.md)）；会话日志已对齐
dsh（时间戳 / header / 目录布局 / 读写句柄 / 未知事件策略，决策
[0051](../../docs/decisions/0051-session-log-alignment.md)）；架构上删掉
`components/`（0052），并把插件间依赖收紧为**只允许由外向内**（0053）；装配单支持
**YAML**（0054）。

**M3 已落地的部分**：六个工具插件（calc / fs / shell / fs-search / web /
ask-user）、token 统计、终端审批、fs 沙箱与读后写策略都在能力目录里；**未做**：
`compact`、`context-meter`、按三类分组的 `plugin-admin` 管理面。共 **536 用例**
离线全绿；另有 **34 条真实 DeepSeek API 功能验收**（`pytest tests/test_real_api.py
--real`，默认跳过）——覆盖流式、提示词送达、角色/override、工具调用、**长链路
（读→改→跑，含失败自纠）**、**多会话并行与同会话多 agent**、**会话内编辑与读后写**、
围栏、会话隔离、重启恢复、用量统计、重试、审批拒绝、工具开关、热插拔、取消与
预算边界，当前 **34/34 通过**。同一批目标另有 28 条离线版本
（`tests/test_feature_goals.py` + `tests/test_long_chain_and_concurrency.py`，
模型走剧本、其余链路全真，含 6 会话 × 5 轮的并发压力与并发 open / 轮次分配的
竞态用例），随默认门禁运行。

**两份可读产物**：`python app.py` 起最小应用；
[docs/ctx-plugin-management.html](docs/ctx-plugin-management.html) 是 ctx 管理插件的
15 步交互图解（浏览器直接打开）。

下一步：M3 收尾（compact / context-meter / 管理面），或直接进 M4 应用化插件
（`web` / `cli` / `sdk`）。

背景（为什么是这两件事）：P3 交出的是一套**可运行的机制集合**（插件内核 +
能力插件 + 构件层），但它是学习形态——每个机制一份目录副本、组合写在代码里，
而且循环与管线策略还长在底座里（P3 收官时如实记录了这三处差距，见
[决策 0040](../../docs/decisions/0040-p3-wrap-up-vs-dsh.md)）。P4 要做的是：

1. **把机制做成产品**：一个项目、一条命令起、能真实干活；
2. **闭上那三处差距**——因为"基础插件"的定义（整个 harness 依赖的最小插件，
   包含 LLM 配置、AgentLoop 等）本身就要求**循环是插件**。

---

## 1. 目标（一句话）

做一个**最小但真实**的插件底座（只做"辅助插件管理"，不要 cordis 的额外功能），
在同一个项目里把 harness 逐步补成**可用产品**：同一份基础插件，换一个应用化
插件（选配）就是另一个产品形态。

## 2. 三类插件（P4 的核心概念）

| 类别 | 是什么 | 判据 | 例子 |
|---|---|---|---|
| **基础插件** `base` | harness 依赖的最小集合 | 卸了它，**任何产品**的 harness 都残缺 | `llm`（模型接缝 + 配置）、`agent-loop`、`sessions`（日志）、`tools`（注册表 + 管线）、`prompt`、`agent`（门面） |
| **应用化插件** `application` | 外层包装：与基础插件配合**形成产品** | 换掉它，**产品形态变了**（网页 / 终端 / SDK / 无人值守），内核不动 | `web`、`cli`、`sdk`（JSON-RPC）、`headless` |
| **能力插件** `capability` | 给 harness 加具体本事 | 装卸它，**产品形态不变**，只是"多 / 少会做一件事" | `tool-*`、`compact`、`token-usage`、`context-meter`、`approval`、`self-tools`、`plugin-admin`、`guard` |

**三者怎么拼出一个产品**：

    产品 = 基础插件（必须有） + 一个应用化插件（选配） + 零到多个能力插件（选配）

两条配套规则（沿承 P3 决策 0035，避免"分类 vs 策略"绕晕）：

1. **分类说的是"插件是什么"，"能不能卸"是装配方按应用判的策略**——`web` 是
   应用化插件，但在"网页产品"这个装配里它就是承载者：角色声明为 `base`，
   台账里不给停用 / 卸载按钮；
2. **一个产品至少要有一个应用化插件**（否则"用户从哪进来"没有答案）。这条在
   P4 里做成**装配期检查**（fail loud），而不是写在文档里靠人记。

分类值沿用 P3 的 `base` / `capability`，新增 `application`；P3 的 `other`
不再需要（演示插件要么算能力插件，要么不装）。

## 3. 底座：边界写死，动工不再讨论

"类似 cordis，但只需要辅助插件管理"落到两张表：

**要（插件管理所需的最小机制）**

| 能力 | 为什么 |
|---|---|
| 服务槽位（provide / require） | 插件之间唯一的协作方式 |
| 事件（emit + waterfall） | 观察与拦截——管线策略要挂在这里（见 M2） |
| effect + disposer | "注册即 effect、卸载自动回卷"：本项目的纪律根基 |
| inject / optional 依赖声明 | 装载顺序无关；依赖变化驱动生命周期 |
| 装 / 卸 / 停用 / 启用 + 台账 | 插件管理本体（状态、依赖、分类、署名） |
| 装配单（选哪些插件 + 各自配置） | "选配形成产品"必须落在**数据**上——P3 的教训：组合写在代码里 = 换产品要改代码 |
| 每个插件可带配置（dict） | 同上；参数不对由插件自己在 setup 里 fail loud |

**不要（cordis 有，但我们明确不用）**

| 不要 | 理由 |
|---|---|
| fork / 隔离作用域 | 单进程单租户，用不上 |
| HMR / 热重载 | 开发期便利，不是产品能力 |
| 配置 schema 校验框架 | 插件构造参数 + setup 里的 fail loud 已够 |
| 反射式 DI / 装饰器元数据 | inject 显式声明更好读也更简单 |
| 定时器 / 日志 / 命令注册等工具集 | 需要什么，各插件自己带 |

## 4. 里程碑

**里程碑不是目录**：都在同一个项目里往前推，做完一个就在下一个上加。每条的
验收都要有测试钉住（含反向验证：撤掉机制即变红）。

### M1 —— 底座（plugin core）

**做**：第 3 节"要"的那张表。

**拆步**（每步"先写验收（测试）→ 实现 → 绿了才下一步"；每步单独评审、单独回退）：

| # | 步 | 这一步钉住的语义 |
|---|---|---|
| 1 | 骨架 + 服务槽位 | provide / get / require / retract；重名与缺失 fail loud |
| 2 | 插件装载与依赖驱动 | 三个假插件打乱顺序结果一致；提供者来去 → 自动激活 / 退位；台账快照可读 |
| 3 | effect 回卷 | 卸根节点服务 / 订阅 / 登记物一个不剩；装载失败不留半装 |
| 4 | 事件 | on / emit / waterfall；退订随 effect 回卷 |
| 5 | 条目与装配单（loader 起步） | Entry（id / name / config / role / enabled）+ 路径 id；factory(**config)；装配期 fail loud |
| 6 | 树与所有权级联 | 卸父先卸子、深的先退；开关以子树为单位 |
| 7 | 热插拔 | update / replace 原子（造新失败不动现场）；revision + 变更报告；变更锁 |
| 8 | 启动器（`core/launcher.py`）与纯度验收 | 空装配单能起；框架模块不认识插件、core 无 harness 词汇（两级扫描）；loader 可停用 |

步 1–4 长的是 **core · 框架**（非插件）；步 5–7 是 **loader**（插件）；步 8 是
**core · 引导**（启动器，`core/launcher.py`——**基座自带启动器**，`boot(装配单)`
点着整棵树）与纯度验收。core · 框架的职责清单（六件）、引导的边界（唯一的
组合根）与"不做"清单见 [决策 0042](../../docs/decisions/0042-p4-base-boundary.md)。

**验收**（三个假插件的最小装配）：
- 装载顺序无关（打乱顺序结果一致）；
- 卸载回卷干净：服务、事件订阅、登记物一个不剩；
- 依赖变化驱动：提供者缺席 → 等待；来了 → 自动激活；走了 → 自动退位；
- 台账快照可读（状态 / 依赖 / 分类）。

### M2 —— 基础插件（harness 最小集，跑通一个真 turn）

**做**：`llm`（模型接缝 + 配置：真实 provider 与假 provider 两条路）、`prompt`、
`sessions`、`tools`（**注册表与执行管线都归它**）、**`agent-loop`（独立插件）**、
`agent`（门面）。

**同时闭上决策 0040 的三处差距**：

1. **循环插件化**：换一个"一步就收尾"的假循环插件，产品照常起；
2. **管线归 `tools`**：注册表与执行管线同一个 owner；
3. **策略层有插座**：超时、预算各做一个小 `guard` 插件；`DenyTools` /
   `TruncateOutput` 这类中间件要么有真实使用者，要么删掉——**不留没有插座的
   机制**。

**验收**：基础插件 + 假 provider 能完整跑一个 turn 并把事件落进日志；上面三条
各有测试钉住。

### M3 —— 能力插件

**做**：工具族（`tool-calc` / `tool-fs` / `tool-search` / `tool-shell`…）、
`compact`、`token-usage`、`context-meter`、`approval`、`self-tools`、
`plugin-admin`（管理面按三类分组）。

**已完成**：`tool-math`（calc / math）、`tool-fs`、`tool-shell`、`tool-fs-search`、
`tool-web-search`、`tool-ask-user`；`llm-tokens-usages`（总量 / 今日按用户 /
本对话，只读视图）；`approval-terminal`；`fs-sandbox` 四种模式 +
`fs-observation-policy`（改前必读）；`proc` 三后端；`workspace-admin`。
**未做**：`compact`、`context-meter`、按三类分组的 `plugin-admin` 管理面。

**验收**：任一能力插件的装卸不牵动别的；停用一件工具模型立即看不到；管理面
三类分组可见可分。

### M4 —— 应用化插件与选配

**做**：三个起步应用 + 装配单：

- `web`：浏览器产品（HTTP 服务 + 页面）；
- `cli`：终端产品（REPL + 一次性 `--ask`）；
- `sdk`：JSON-RPC 服务（外部程序接入）。

**验收**：同一份核心 + 三份装配单 → 三种产品；**不改一行代码**就能换；
"至少要有一个应用化插件"的装配期检查 fail loud。

### M5 —— 可用产品（实质化验收）

**做**：打包与启动器（一条命令起产品）、README（能教会别人从零跑起来）、
真实模型端到端演练。

**验收（也就是 P4 整体的验收）**：
- **Dogfood**：用它完成一件真实的活（读代码 → 改文件 → 跑命令 → 写测试），
  全程走产品入口，不碰内部 API；
- 三形态（web / cli / sdk）都能起、都能对话、都能干活；
- 门禁绿（ruff + pytest）；决策记录齐。

## 5. 与 P3 的关系（**全部重写**：P3 只作参考，不搬代码）

P4 的每一行从零写。P3 不提供代码，提供两样东西：

- **语义对照**：同一个机制 P3 怎么实现的——写之前读一遍，确认没漏掉语义
  （如 optional 的"缺了照常激活、来了不重装"，退位顺序"深的先退"）；
- **教训清单**：P3 决策记录里的坑（0031 声明纪律 / 0035 角色归属 / 0038
  loader 三处教训 / 0039 端口与面板），写每步前先读相关记录。

P3 的 423 个用例不复用，可当"边角情况清单"查；阶段副本 `_01`–`_06` 与
`mini_harness/` 留在 P3 不动。为什么不搬、代价是什么：见
[决策 0043](../../docs/decisions/0043-p4-rewrite-from-scratch.md)。

## 6. 待定（动工时定，本文不拍板）

- ~~底座是"搬 P3 kernel 后收敛"还是"照边界重写"~~ —— **已定（2026-10-16）：
  照边界从零重写，逐个小步**（决策 0043）；
- `cli` 产品：纯 REPL，还是上 TUI；
- ~~装配单格式（YAML / TOML / Python 字面量）~~ —— **已定（2026-10-19）：
  YAML 是数据、插件工厂白名单留在代码**（决策 0054）；Python 字面量保留给
  "需要函数"的离线剧本（`transport` 表达不了），patch / overlay 仍待定
  （P3 曾推迟，见决策 0029 一带的讨论）。

---

_相关：[P4 定调决策 0041](../../docs/decisions/0041-p4-substantiation.md)、
[P3 收官与三处差距](../../docs/decisions/0040-p3-wrap-up-vs-dsh.md)、
[P3 阶段计划](../P3_Coding/PLAN.md)、[根 README](../README.md) 的路线图。_
