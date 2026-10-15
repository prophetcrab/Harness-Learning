# P3_Coding

P3 阶段的编码工作区。**组织方式沿用 P2：阶段式工作区**——`mini_harness/` 是
基线/整合包（当前 = `_05` 完成态），`_01`–`_11` 每阶段一个自包含目录、
只加一个机制，从上一阶段完成态复制起步。

**当前进度**：基线已落地（完全插件化 + 界面 + RPC）；`_01`（自扩展工具 +
浏览器审批 + 从工作区安装插件）、`_02`（流式输出 + 取消）、`_03`（用量与
预算）、`_04`（上下文仪表：容量 / 构成 / 缓存命中）、`_05`（上下文压缩：
日志折叠声明 + 重放同构，默认窗口 1M）**均已完成**；`_05` 收尾补做了
**插件管理**（分类台账 + 一键 安装/卸载/停用/启用）与 **turn 执行器**
（并行会话 / 刷新不掉 / 可中断）。`mini_harness/` 基线已按 PLAN 同步到
`_05` 完成态（**344 用例全绿**）。

> P3 的路线定位见根 [README.md](../README.md) 的"未来路线图"。按计划，P3 的
> 能力范围应由"垂类"裁剪；先交付**地基**，后续能力（自扩展、流式、预算计量、
> 压缩、任务管理……）在这个底座上以插件形式增量补。决策记录见
> [docs/decisions/0020](../docs/decisions/0020-p3-plugin-core.md)。
>
> **阶段计划见 [PLAN.md](PLAN.md)**：`_01`–`_11` 每阶段加一个机制；`_01`–`_05`
> 与垂类无关（已完成），`_06` 起按垂类裁剪。

## 目录

| 目录 | 角色 |
|---|---|
| `mini_harness/` | **基线/整合包**（自包含可运行；里程碑处从最新阶段重生成） |
| `_01_Self_Plugin_Tools/` | 阶段目录：自扩展工具 + 手动审批 ✅ 完成 |
| `_02_Streaming/` | 阶段目录：流式输出 ✅ 完成 |
| `_03_Usage_Budget/` | 阶段目录：用量与预算 ✅ 完成 |
| `_04_Context_Meter/` | 阶段目录：上下文仪表 ✅ 完成 |
| `_05_Compaction/` | 阶段目录：上下文压缩 ✅ 完成（基线在此重生成） |
| `scripts/check.py` | 门禁：ruff + 逐目录 pytest（未装 mypy 则跳过） |
| `pyproject.toml` | ruff 配置（整个 P3 一份） |

---

## 1. 这一包解决什么问题

P2 走到了"服务化"，但装配仍是配置树 + 工厂注册表：**能力之间怎么依赖、
装错了怎么办、运行时能不能换**，都由装配代码隐含决定。

P3 基础包把这件事反过来：**内核只认"插件"和"它声明依赖的服务"**。

- 每样能力都是一个插件（`setup(ctx)` + `name` + `inject`）；
- 装配 = 列插件；**装载顺序无关**——依赖齐备的那一刻插件自动激活；
- 卸载 = 回卷：插件装出来的服务、事件订阅、外部注册全部自动撤销；
- 依赖消失（提供者被卸）→ 依赖者自动停用；提供者回来 → 自动重新激活。

一句话：**"装哪些能力"从装配代码的隐含条件，变成内核可检查、可回卷的事实。**

## 2. 三层结构（依赖单向）

```
plugins/     能力插件：把构件装成服务（llm / fs / proc / tools / sessions / prompt / agent / webui / rpc）
    ↓ 只 import 下两层
components/  纯构件：从 P1/P2 沉淀的可复用实现（不 import kernel，不认识"插件"）
    ↓ 无依赖
kernel/      插件内核：ctx（服务 / 事件 / effect）+ 插件生命周期（inject 驱动）
```

| 层 | 文件 | 内容 |
|---|---|---|
| `kernel/` | `context.py` | **Context**：服务槽位（provide/retract/require/get）、事件（on/emit/waterfall）、effect 台账（装载即登记、卸载自动回卷、子先父后）、插件生命周期 |
| | `plugin.py` | **Plugin** 协议与声明（name / inject / optional / category / setup 的读取） |
| `components/` | `llm/ session/ prompt/ tools/ fs/ proc/ agent/ context/ shell/ rpc/` | P1/P2 的构件原样沉淀（provider 接缝、事件日志、提示词装配、工具管线、能力实现、主循环、JSON-RPC 帧与传输）；`session/projection.py` 合并了 P2 的 `system/message` 遮蔽 |
| | `agent/runner.py` | **turn 执行器**（`_05` 收尾）：turn 跑在后台线程、HTTP 请求只是订阅者——并行会话 / 刷新接回 / 可中断 |
| | `context/tokens.py` | token 估算与请求构成拆分（仪表与压缩共用） |
| | `shell/` | 界面契约：`PanelSpec` / `DisplaySpec` / `RouteSpec` + 统一发现（插件自述"我有个页面 / 我的事件怎么画 / 我的端点"，壳零知识） |
| `plugins/` | `llm.py fs.py proc.py tools.py sessions.py prompt.py agent.py` | 七个能力插件 + 服务名/事件名常量 + `boot.py` 标准装配 |
| | `catalog.py` | 可安装白名单（模型侧 `self-tools` 与界面侧 `plugin-admin` **共用同一份**） |
| | `plugin_admin.py` | ★ 插件管理：分类策略 + 一键 安装/卸载/停用/启用（策略在这里，界面保持哑） |
| | `webui.py` + `webui/` | ★ **界面插件**（本机 HTTP 服务 + 零依赖页面三件套）——装载即起、卸载即停 |
| | `rpc.py` | ★ **RPC 插件**（JSON-RPC 服务端：initialize / prompt / follow；stdio 或 TCP）——装载即起、卸载即停 |

`components/` 是本包唯一"搬过来"的部分（来自 P2 冻结基线；只改 import 与两处小升级：
`ToolRegistry.unregister`、事件表加 `system/message`）。

## 3. 插件内核的五件语义

**服务（单槽）**：一个服务名只有一个提供者；重复提供直接报错。
`ctx.get` 是可选依赖（没有就 None），`ctx.require` 是必需依赖（没有就报错）。

**注册即 effect**：装载期（setup 执行中）做的一切——`provide`、`on`、嵌套
`effect`——自动登记撤销动作。插件不必写"我装过什么"，卸载时逐层回卷
（子先父后、同级后进先出）；**装载失败不留半装状态**；某个 disposer 抛错
不打断其余回卷。

**inject 驱动生命周期**：`inject = ("fs",)` 声明依赖。装载顺序无关；依赖
缺失保持 pending（`ctx.pending_plugins` 可查）；提供者被卸载时依赖者**先**退位
（它的清理代码运行期间服务仍可用），提供者回来时自动重新激活。

**事件**：`ctx.on` 订阅（返回退订函数，装载期订阅随 effect 回卷）；
`ctx.emit` 广播（只读通知）；`ctx.waterfall` 瀑布（handler 返回非 None 即改写值）
——后者是插件协作的载体：`prompt/variables` 让插件注入运行时变量，
`llm/request` 让插件改写模型请求。

**boot**：`boot(plugins=[...])` 装载并检查"没有残留 pending"（依赖写错
fail loud）；`Booted.close()` 逆序卸载全部插件。

## 4. 插件清单

下表的 15 个能力插件 + 1 个示例，是 P3 现有的全部插件。**默认装配装其中 11 个**
（`boot_offline` / `boot_realtime`：`llm` `fs` `proc` `sessions` `prompt` `tools`
`token-usage` `plugin-admin` `context-meter` `compact` `agent`）；其余按需装：
`--self-tools` 加 `self-tools` + `web-approval`，`--web` 加 `webui`，`--rpc` 加 `rpc`，
`clock` 只在演示里。装哪些都能在页面的插件台账面板里一键改。

| 插件 | 提供 | inject | 说明 |
|---|---|---|---|
| `llm` | `llm` | — | FakeLLM（严格剧本，测试）/ RepeatingLLM（固定回复，交互）/ DeepSeek；调用前过 `llm/request` 瀑布，调用后广播 `llm/response` |
| `fs` | `fs` | — | LocalFS / MemoryFS / WorkspaceJailFS（插件名带实现后缀） |
| `proc` | `subprocess` | — | LocalSubprocess / ScriptedSubprocess（**可选**能力） |
| `tools` | `tools` | `fs` | 组装工具面（8 件：calculate + read/write/list/edit + search/find + 可选 shell）；卸载时注销注册表 |
| `sessions` | `sessions` | — | 打开即恢复 + 崩溃尾行修复 + 列举 + 分叉 |
| `prompt` | `prompt` | — | section 装配 + `prompt/variables` 瀑布 + 装配单 |
| `agent` | `agent` | `llm` `tools` `sessions` `prompt` | 会话管理：`ask(text, session_id)` 跑一个 turn；**每条会话一把锁**（同会话串行、跨会话并行） |
| `webui` | `webui` | （可选）`agent` `sessions` | ★ **浏览器界面**：本机 HTTP 服务；**装载即起、卸载即停**（端口释放）；插件台账弹出面板 / 会话 / 实时事件流 |
| `rpc` | `rpc` | `agent` `sessions` | ★ **JSON-RPC 服务端**：`initialize` / `session.prompt` / `session.follow`（重放+订阅）；stdio 或 TCP（`listen=` 装载即起、卸载即停） |
| `self-tools` | `self-tools` | `tools` `fs` | `_01`：让模型装卸白名单插件（`install_plugin` 需人工审批） |
| `web-approval` | （审批策略） | — | `_01`：审批点在浏览器里，问不到人则 fail-closed；审批**按线程**登记推流通道 |
| `token-usage` | 账本 + 页面 | `tools` | `_03`：用量账本 + 用量页面（8767） |
| `context-meter` | 仪表 + 页面 | — | `_04`：上下文构成 / 容量（8768） |
| `compact` | `compacter` | `llm` | `_05`：上下文压缩（90% 触发、日志只追加折叠声明） |
| `plugin-admin` | 管理 + 端点 | — | `_05` 收尾：分类台账 + 一键 安装/卸载/停用/启用 |
| `clock` | — | — | 示例可装卸插件（不在默认装配里） |

> **每个插件的角色（`category`：`base` / `capability` / `other`）由装配方定**，
> 插件自己的声明只是默认值："它是不是地基"是**应用的性质**——`webui` 在终端
> 组合里是可选的观察窗口（关掉还能用命令行说话），在 `--web`（界面即产品）里
> 就是入口本身。内核只读出来记进台账，**策略归 `plugin_admin`**（基础插件不给
> 卸载/停用按钮）。见 [0033](../docs/decisions/0033-plugin-admin.md)（策略）与
> [0035](../docs/decisions/0035-assembly-declares-role.md)（判断权归谁）。

**界面与 RPC 都是插件**（`plugins/webui.py`、`plugins/rpc.py`）——它比多数能力更能说明 effect 的价值：
HTTP 服务天然带生命周期（线程 + 端口），P2 时代的页面服务是手工起的、
没有卸载路径；现在

```python
ctx.load_plugin(webui_plugin(port=0))   # 起服务；port=0 由系统分配空闲端口
ctx.require("webui").url                # http://127.0.0.1:<port>/
ctx.unload_plugin("webui")              # 停服 + 端口释放，零残留
```

三条实现纪律（代码注释里也写了）：**生命周期只在主线程做**（内核无锁），
服务线程只调服务、不改 ctx；**turn 跑在后台线程里、HTTP 请求只是订阅者**
（`components/agent/runner.py`：不同会话并行、同会话串行、刷新页面接回同一个
turn、可中断）；**缺依赖装载期就报错**，而不是打开页面才 500。

RPC 插件把这套语义给了**协议面**（跨进程、多客户端）：

```python
ctx.load_plugin(rpc_plugin(listen=0))   # 装载即起 TCP（默认只听本机；0 = 系统分配）
ctx.require("rpc").address              # ('127.0.0.1', <port>)
ctx.unload_plugin("rpc")                # 卸载即停（端口释放 + 订阅台账清空）
```

**`session.follow` = 重放 + 订阅**：先重放日志里 `seq > from_seq` 的既有事件，
再登记实时推送——**推送粒度是"每条会话事件"**（长 turn 进行中也能看到事件流
生长），来源是 `sessions` 插件在每次 append 后发出的 `session/event` 内核事件。
断线重连带 `from_seq` 即补齐：**补缺靠重放日志，不靠缓存**（事件溯源的红利）。
stdio 模式有一条硬纪律：**stdout 只出帧**（banner/台账全走 stderr），
有真实子进程的回归测试钉住。

## 5. 运行方法

```bash
cd P3_Coding/mini_harness
python -m pytest -q                       # 344 个用例（全离线）

# 离线组合（默认）：离线模型 + MemoryFS + 剧本命令
python chat.py
python chat.py --ask "帮我算 2+3"
python demo.py                            # 八节演示：装配 / 台账 / 对话 / resume / 热插拔 / 回卷 / 界面插件 / RPC

# ★ 界面（离线；装载即起服务、Ctrl+C 卸载退出）
python chat.py --web                      # 自动开浏览器；--port 0 让系统分配端口
python chat.py --web --realtime           # 换成真实 DeepSeek（需项目根 .env）
python chat.py --web --realtime --self-tools   # 再带上自扩展 + 浏览器审批
./run.bat web                             # 启动器等价入口

# ★ RPC 服务（stdio JSON-RPC；--listen N 同时开 TCP，多客户端共享会话）
python chat.py --rpc
python chat.py --rpc --listen 8771
./run.bat rpc --listen 8771               # 启动器等价入口

# 真实组合：DeepSeek + 真实磁盘 + 真实进程（读项目根 .env 的 key）
python chat.py --realtime
python chat.py --realtime --fs jail --ask "用 shell 看看当前目录"
python chat.py --realtime --no-approve    # 需审批操作自动放行（谨慎）

# 一键启动器
./run.bat            # 离线 demo；./run.bat chat 对话；./run.bat test 跑测试
bash run.sh chat --realtime
```

会话内命令：`/plugins`（插件台账与状态）、`/services`、`/tools`、`/history`、
`/web`（现场装卸界面插件）、`/exit`。

**门禁**（在 `P3_Coding/` 下）：`python scripts/check.py`（ruff + pytest；mypy 可选）。

## 6. demo 与界面里看得到的东西

`python demo.py` 八节，全离线：

```
0) 装载：故意打乱插件顺序 → 全部就绪（inject 驱动，顺序无关）
1) 台账：插件 / 服务 / 8 件工具面
2) 对话：一个 turn 的完整事件流（含工具调用）落盘 session.jsonl
3) resume：重新装配后同名会话续聊，turn 编号接续
4) 热插拔：卸载 fs → tools/agent 自动退位（pending）；换入 LocalFS → 自动重激活
5) 卸载：整体回卷——服务、订阅、工具面全部归零，无残留
6) 界面插件：装载即起服务（HTTP 可访问）→ 卸载即停服（端口真的被释放）
7) RPC 插件：握手门禁 → prompt → follow（重放 + 订阅、跨连接实时推送、from_seq 补齐）
```

`python chat.py --web` 打开页面（P3 形态）：左栏是**插件台账的入口门卡**
（显示活跃数，点开是独立弹出面板：按 基础/能力/其他 分组、可筛选、一键
安装/卸载/停用/启用），中栏对话（NDJSON 流式推事件，边跑边长），右栏是该会话的
**append-only 事件流**（历史由日志投影，与命令行看到的一致）。

**并行与接回**（`_05` 收尾加的）：不同会话的 turn 真的同时跑——界面左栏的会话名
前缀一个脉冲绿点表示"运行中"；运行中再发消息不会静默吞掉，而是弹出选择条
（中断并重新发送 / 等它跑完 / 取消）；**刷新页面不影响运行中的 turn**，页面会
自动接回并继续显示，正在等待的审批卡片也会补回来。

## 7. 与 P2 的关系

- **能力等价**：P2 能做的（提示词装配与追溯、三条接缝、会话日志、工具面、
  审批 fail-closed、离线/真实双组合、可视化页面）本包都有，工具面同为 8 件；
- **装配变了**：P2 用 YAML 配置树 + 工厂注册表；P3 用**插件列表 + inject**。
  P2 的配置分层（`profiles` / `--dump-config`）尚未移植——它是"配置数据 → 插件"
  的一层，在 P3 底座上重做属于后续工作；
- **界面与 RPC 都回来了，且都是插件**：P2 的 `webui/` 与 `serve.py` 是手工起的
  服务；P3 里 `webui` / `rpc` 两个插件都有完整的装载/卸载语义（含 TCP 端口释放、
  订阅随连接注销）。

## 8. 已知边界（诚实记录）

- **没有重试与错误分类**：provider 调用失败直接抛给循环，没有退避重试；
- **没有 spill**（超大工具结果转存）与 **guard**（重复调用/空转检测）——PLAN 的
  `_07` / `_08`；
- **没有任务管理**（goal / plan / todo）——`_06` 曾实现又按用户要求整体回退，
  见下方"进度说明"；
- **中断是协作式的**：正在执行的工具调用不会被砍断（半截命令比等几秒更糟），
  一个 30 秒的 shell 要跑完才响应取消；
- **turn 不随请求结束而结束**：客户端断开（含刷新）后 turn 会跑完——这正是
  "刷新不掉进度"的实现方式，代价是"人走了，账单还在涨"（要真停得按中断）；
- **同会话不排队**：第二条消息要么等它跑完、要么中断它，没有第三条路；
- **流式请求不复用连接**（`Connection: close`）：每次流式新建一条 TCP，本机可忽略；
- **界面与 RPC 的边界**：只绑本机、无鉴权；webui 的事件流只推给**当前订阅者**
  （可多个、可随时来走），需要跨进程断线补齐请走 `rpc` 插件的 `session.follow`；
- **没有随附 attach 客户端**（P2 的 `attach.py` 未移植）：实时跟随可以直接用
  `chat.py --rpc --listen N` + 任意 JSON-RPC 客户端；
- **级联重启**：换底层插件（如 fs）会让依赖链上的插件一并退位；`webui` 因为声明
  的是 **optional** 依赖而免疫（换模型/装卸插件时界面不断线，见 `_05` 的界面重构）；
- 插件依赖是"服务名存在性"检查，没有版本约束、没有环形依赖检测
  （环形会成组退位，不会死循环，但语义未定义——见决策记录"后果"）。

## 9. 进度说明：`_06` 的两次回退

`_06` 在 PLAN 里写的是**任务管理**（todo）。实际做过两轮，都按用户要求撤掉了：

1. **goal 模式**（`50921a5` 撤回）：先做了一版持久目标（`/goal` 命令 + 状态进
   日志），用户判断"还没优化完"，要求回到 PLAN 原本的顺序；
2. **todo 清单**（`1a86778` 全部 Revert）：按 PLAN 做了 `todo` 工具 + `/todo`
   人命令，随后用户报告"正常对话功能都无法使用"——回退整个 `_06`。

**代价与教训**：`_06` 的目录已不存在，代码状态 == `_05` 完成态。回退后先做了
两批"收尾"（`_05` 的三处耦合修复、壳的两条扩展点、插件管理、turn 执行器），
把地基上的欠账补完，再考虑 `_06` 起的能力。

> 若要重做 `_06`：先按项目纪律把验收标准写细，并**先在 `_05` 的基线上跑通
> 现有 344 个用例**再动代码；上两轮的问题都出在"机制穿透了既有链路"
> （todo 的快照写入打乱了正常对话的事件流）。
