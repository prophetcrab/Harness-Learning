# Harness 开发学习计划

> 目标：从零掌握 agent harness 的开发。
> 参考实现：**DeepSeek Harness**（`D:\project\deepseek-harness-master`，TypeScript，下文简称 `dsh/`）。
> 本项目实现语言：**Python 3.13**（复用 `AgentDevLearn/.venv`）。
> 本文件是学习进程的唯一驱动文档：**每个阶段先定义可运行的验收，再开始写代码；验不过，不进入下一阶段。**

---

## 0. 怎么用这份计划

**学习循环（每个阶段重复）**

```
读参考代码 → 写验收（测试或 demo 命令）→ 实现 → 跑通 → 写决策笔记 → 过门禁 → commit → 下一阶段
```

**三条工程硬约束**

1. **无验收不开始** —— 动手前先写下本阶段的验收命令（pytest 用例或 CLI demo），它就是"完成"的定义。
2. **无笔记不算完成** —— 阶段结束必须留下 `docs/decisions/NNNN-*.md` 决策记录（背景 / 决策 / 备选 / 后果，模仿 dsh 的 Agent Notes）。
3. **门禁不绿不前进** —— `python scripts/check.py` 必须全绿（ruff + mypy + pytest）。

**为什么用代码和工程驱动**

- harness 的难点不在概念名词，而在机制细节：事件顺序、取消语义、持久化一致性、provider 替换后行为不变。只有写出来、跑起来、把进程杀掉再恢复，才算真正理解。
- 本计划的每个阶段都对应 dsh 中一个真实工程问题；你要写的是它的"最小可运行版本"，而不是玩具示例。

**关于 AI 结对**

- `dsh/` 代码读不懂时，让 AI 解释（给出文件路径 + 具体问题）。
- 本项目代码尽量自己写；卡住 20 分钟以上再求助。完成后让 AI 做评审、补测试、出验收。

---

## 1. 目标形态与技术选型

M7 结束时，mini-harness 具备：

| 能力 | 说明 |
|---|---|
| Agent 主循环 | turn/step 驱动、流式输出、工具调用闭环、可取消 |
| 工具系统 | 注册表 + JSON Schema + pre/execute/post 防护管线 + 审批 |
| 会话日志 | append-only 事件溯源、JSONL 落盘、resume/fork、崩溃修复 |
| 提示词装配 | section 注册、变量插值、运行时上下文、可追溯 |
| 能力接缝 | fs / shell / llm 均为 Definition/Provider/Consumer 三角色，可整体替换 |
| Profile 组装 | YAML 分层 patch、`dump-config` 可见、配置错误 fail loud |
| 服务化 | JSON-RPC 服务 + 事件流 follow、CLI attach（可选 Web） |

**技术选型及理由**

- **Python 3.13**：聚焦 harness 机制，不被构建工具链分心；`.venv` 已就绪。
- **dsh 只读不抄**：TS 只需能读懂类型、事件流和类之间的关系。用另一种语言重写，能强制你理解而不是复制。
- **依赖极简**：`openai`（DeepSeek API 兼容 OpenAI 协议）、`pydantic`（schema/配置校验）、`pytest` + `pytest-asyncio`；工具链 `ruff` + `mypy`。
- **LLM 从第一天就是接缝**：`FakeLLM`（离线、可断言的多步剧本）+ `DeepSeekLLM`（真实调用）。所有测试默认跑 FakeLLM。

---

## 2. 目标仓库结构

```
AgentDevLearn/
├── docs/
│   ├── learning-plan.md        ← 本文件（进度表在文末）
│   ├── decisions/              ← 决策记录（NNNN-标题.md）
│   └── notes/                  ← 阶段学习笔记
├── harness/                    ← 实现代码
│   ├── __main__.py / cli.py    ← python -m harness
│   ├── llm/                    ← M1：LLM 接缝 + Fake/DeepSeek provider
│   ├── agent/                  ← M1：主循环（turn/step、inbox）
│   ├── tools/                  ← M2：注册表 + 管线 + 内置工具
│   ├── session/                ← M3：事件日志 + JSONL 持久化
│   ├── prompt/                 ← M4：提示词装配
│   ├── providers/              ← M5：fs / shell / subprocess 接缝
│   ├── config/                 ← M6：profile 组装
│   └── server/                 ← M7：RPC + 事件流
├── profiles/                   ← M6：YAML 组合层
├── scripts/check.py            ← 本地门禁
├── tests/
├── pyproject.toml
└── .venv/
```

目录随阶段生长；每个阶段只新建自己需要的那部分。

> **落地位置**：M1–M3 的实际产出在 `P1_Coding/`（五个自包含练习目录）。
> 上面这棵 `harness/` 包结构自 **P2** 起建立：P2 把 P1 的四份副本收敛成单一 `harness/` 基线，
> 并补上 `pyproject.toml` + `scripts/check.py` 门禁；然后按「**每阶段一个最小扩展**」拆成
> `_01`–`_11` 十一个自包含目录（每个目录自带一份完整 `harness/` 副本），M4–M7 的机制
> 分别在其中一次加一个。

---

## 3. 贯穿全程的铁律

从 dsh 提炼的 8 条不变量。每做一个设计决定前，先对照这张表：

| # | 铁律 | 含义 | dsh 出处 |
|---|---|---|---|
| 1 | 模型可见 ⟺ 已记录 | 能到达模型的输入必须能从日志重建；新增模型可见输入 = 新增事件 | `docs/architecture.md` 的 "Model-visible means logged" |
| 2 | 日志只增不改 | 修改/压缩用新事件遮蔽旧事件，绝不重写历史 | `core/session` 的 append-only + surface replace |
| 3 | 一切注册都是 effect | 注册动作返回 disposer，卸载时自动回卷 | Cordis `ctx.effect()` |
| 4 | 注册表分层 + 遮蔽 | 全局层 + 作用域覆盖层；同名遮蔽（most-specific-wins） | `core/scope` |
| 5 | 接缝 = Definition + Provider + Consumer | 三角色齐全才算一个能力；单槽服务重复注册直接报错 | `docs/capability-seams.md` |
| 6 | 显式解析优于隐式默认 | 默认值在显式的 resolve 步骤给出，不藏在执行函数里 | dsh `AGENTS.md` |
| 7 | 错误是给模型的输入 | 工具失败转成结构化结果回给模型，而不是中断循环 | tools 执行管线 |
| 8 | fail loud | 配置/装配错误在启动时立刻报错，不静默跳过 | 同上 |

---

## 4. 阶段计划

### M0 起步：工程地基（0.5–1 天）

**目标**：仓库能跑、能测、能提交；建立 harness 的全局心智模型。
**读**：`dsh/README.md`；`dsh/docs/architecture.md`（重点读 Turn flow 一节）；`dsh/docs/glossary.md`。
**做**：

- `git init`；写 `pyproject.toml`；安装依赖（见 §7）；`pip install -e .`。
- `harness/__main__.py`：`python -m harness --help` 打印版本与占位子命令。
- `tests/test_smoke.py`；`scripts/check.py` 依次跑 ruff / mypy / pytest。
- 第一份决策记录：`docs/decisions/0000-技术选型.md`（为什么 Python、为什么不抄 TS）。

**验收**：`python -m harness --help` 正常；`python scripts/check.py` 全绿；`git log --oneline` 有 commit。

### M1 最小 Agent Loop：闭环（3–5 天）

**目标**：打通「模型 → 工具调用 → 工具结果 → 模型 → 最终回答」；建立 turn/step 词汇。
**读**：`dsh/packages/core/agent-loop/src/agent.ts`（turn/step 主流程）、`inbox.ts`（输入队列）、`tool-calls.ts`；`dsh/packages/llm/llm/src/message.ts`（消息词汇）；`dsh/docs/agent-lifecycle.md`。
**做**：

- `llm/`：`LLMProvider` 协议（`stream(messages, tools) -> chunks`）；`FakeLLM`（脚本化多步回复，测试用）；`DeepSeekLLM`（流式，读取 `DEEPSEEK_API_KEY`）。
- 消息词汇：`system` / `user` / `assistant` / `tool` 四类消息 + `tool_call` 结构。
- `agent/loop.py`：组装消息 → 调用模型 → 若有工具调用则执行并以 `tool` 消息回填 → 直到无工具调用；`max_steps` 上限；流式打印。
- 一个工具：`calculator`。
- CLI：`python -m harness run "23*17 等于多少"`。

**验收**：

- demo 中确实发生「tool_call → 结果 → 最终回答」，而不是模型心算。
- FakeLLM 测试覆盖两步收尾；超出 step 预算时返回结构化终止而不是死循环。
- `docs/decisions/0001-turn-and-step.md`：为什么模仿 dsh 区分 turn 与 step。

### M2 工具系统：注册表 + 防护管线（4–6 天）

**目标**：工具从"一个 if"长成 dsh 式的注册表与执行管线。
**读**：`dsh/packages/core/tools/src/index.ts`；`dsh/docs/tool-execution-pipeline.md`；`dsh/docs/cookbook/adding-a-tool.md`；`dsh/packages/interaction/user-approval/src/index.ts`；`dsh/packages/guard/`（repeat-tool-reminder、timeout-policy）。
**做**：

- `tools/registry.py`：注册/查询；先两层（全局 + 会话级），同名遮蔽。
- Schema：用 pydantic 模型生成 JSON Schema，装配进模型请求。
- `tools/pipeline.py`：`pre`（allow/deny/ask、可改写参数）→ `execute`（含超时）→ `post`（可替换内容、附加上下文）；用"中间件链"实现。
- 审批：危险工具走 CLI y/n；无应答方时 fail-closed 拒绝。
- 内置工具：`read_file` / `write_file` / `shell`（subprocess 捕获输出）。

**验收**：

- 测试三件套：pre 拒绝短路执行；超时返回 `TOOL_TIMEOUT`；工具异常转结构化错误回给模型。
- demo：`python -m harness run "用 shell 看当前目录"` 触发审批，输入 n 后模型拿到拒绝原因并合理收尾。
- `docs/decisions/0002-tool-pipeline.md`：为什么用管线而不是散落的 if。

### M3 会话日志：事件溯源（5–7 天）★ 全项目的心脏

**目标**：模型历史不再是"内存里的消息列表"，而是日志的投影。
**读**：`dsh/packages/core/session/src/types.ts`（事件词汇表）、`index.ts`（append/校验/冻结）、`surface.ts`（消息投影）；`dsh/packages/session/session-persistence-jsonl/src/`（storage、generation：落盘/代际/崩溃修复）；`dsh/docs/architecture.md` 的 Session log 一节。
**做**：

- 事件模型：`turn/start|end`、`step/start|end`、`user/message`、`assistant/message`、`tool/call`、`tool/result`。
- `Session.append()`：单调 seq、深拷贝冻结、写入前校验。
- JSONL 持久化：`<root>/<session_id>/session.jsonl`；每条 append + flush；**启动时检测并修复被截断的尾行**（模拟 kill -9）。
- `derive_messages()`：从日志投影出模型历史。**消息永远由日志派生，绝不直接存。**
- `resume(session_id)`、`fork(session_id, upto_seq)`。
- CLI：`python -m harness run --session <id>`；`python -m harness sessions list|show`。

**验收**：

- 测试："重放日志得到的消息历史 == 在线产生的历史"（同构断言）。
- demo：对话中途 Ctrl+C，`sessions list` 能看到它，resume 后继续且历史完整。
- 手工实验：向 jsonl 尾部写半行 JSON，重启自动截断修复且已确认事件不丢。
- `docs/decisions/0003-event-sourcing.md`：为什么消息用投影而非直接存储（写不出理由就回去重读）。

### ★ P2 阶段：M4–M7（工作区 `P2_Coding/`）

以下 M4–M7 归为 **P2 阶段**，在 `P2_Coding/` 里以**阶段式工作区**推进——
延续 P1「一个阶段只加一个机制」的思路，把 M4–M7 拆成 **11 个可独立验收的小阶段**，
每个阶段一个自包含目录（各自带一份完整 `harness/` 副本）：

`_01_Prompt_Sections`(M4) → `_02_Prompt_Context`(M4) → `_03_Prompt_Trace`(M4) →
`_04_Filesystem_Seam`(M5) → `_05_Workspace_Jail`(M5) → `_06_Subprocess_Seam`(M5) →
`_07_Plugin_Effect`(M6) → `_08_Profile_Layers`(M6) → `_09_Dump_Config`(M6) →
`_10_Rpc_Transport`(M7) → `_11_Session_Follow`(M7)。

实现写在各自目录的 `harness/prompt`、`harness/providers`、`harness/config`、`harness/server`
子包里；`pyproject.toml` + `scripts/check.py` 提供门禁（逐阶段跑）。
详见 [P2_Coding/README.md](../P2_Coding/README.md)。

### M4 系统提示与上下文（3–4 天）

**目标**：提示词是可组合、可追溯、可重建的产物。
**读**：`dsh/packages/core/system-prompt/src/index.ts`；`dsh/packages/context/`（workspace 指令、时间上下文）；`dsh/docs/subsystems/system-prompt.md`。
**做**：

- section 注册表（有序、支持作用域覆盖）；变量插值 `{{cwd}}`、`{{platform}}`、`{{time}}`。
- 运行时上下文每 step 渲染；`system/message` 进日志。
- 装配断言：渲染出的提示词必须能由「日志 + 装配器」重建。
- CLI：`python -m harness run --dump-prompt` 打印装配过程与来源。

**验收**：快照测试稳定；改 cwd 只影响对应 section；新增一个 section 不动其他部分；`docs/decisions/0006-prompt-assembly.md`。

### M5 能力接缝：Provider 替换（4–6 天）

**目标**：把 M2 里"能用"的文件/命令工具重构成三角色接缝，体验"换 provider 换产品"。
**读**：`dsh/packages/shell/shell/src/index.ts` + `bash-local/` + `tool-bash/`（接缝的教科书样例）；`dsh/packages/fs/fs/src/index.ts`；`dsh/packages/subprocess/subprocess/src/index.ts`；`dsh/docs/capability-seams.md`（那张自动生成的服务-实现-消费者图）。
**做**：

- 定义抽象 `FileSystem`（read/write/edit/列表）与 `SubprocessService`（spawn/捕获）；本地实现；工具只依赖抽象。
- 单槽服务：重复注册即报错；provider 选择在显式 resolve 步骤完成。
- 至少两个新 provider：`MemoryFS`（测试用）；`WorkspaceJailFS`（策略：越出工作目录的写被拒）。
- 配置里选择 provider（为 M6 铺路）。

**验收**：同一套工具测试在 `local` 与 `memory` 下全绿；jail 越权错误结构与其他错误一致；`docs/decisions/0007-capability-seams.md`（三角色边界怎么切）。

### M6 组合与配置：Profile 式组装（3–5 天）

**目标**：能力组合从代码变成配置数据；一行 patch 完成 provider 替换。
**读**：`dsh/packages/boot/app-boot/src/profile.ts`；`dsh/packages/bundle/base/cordis.patch.yml`（共享底座补丁长什么样）；`dsh/apps/cli/src/profile-boot.ts`（补丁叠加顺序）；`dsh/docs/cordis-primer.md`（loader configuration 节）。
**做**：

- 极简插件协议：`Plugin.setup(ctx) -> disposer`；注册即 effect。
- `profiles/*.yaml`：有序层 + 按 id patch（替换整块 config 或 insert 新行）；顺序：base → profile patch → 用户 patch → CLI `--patch`。
- `python -m harness --profile <name> dump-config`。
- 两个 profile：`dev`（FakeLLM + MemoryFS）、`prod`（DeepSeek + 本地）。

**验收**：同 base 两个 profile 产出不同且可读的树；换 LLM provider 只改一行；故意写错配置启动即报错并指出位置；`docs/decisions/0008-profile-composition.md`。

### M7 服务化与多前端（5–7 天）

**目标**：harness 成为常驻服务；前端通过事件流跟随会话（断线可补）。
**读**：`dsh/packages/sdk/protocol/src/transport.ts`（换行 JSON-RPC）；`dsh/packages/api/gateway/src/index.ts`（RPC 网关）；`dsh/packages/host/webserver/src/index.ts`；`dsh/packages/client/connection/`（流式跟随、重连）；`dsh/docs/api-gateway.md`。
**做**：

- `python -m harness serve`：先做 stdio 换行 JSON-RPC（`initialize` / `session.prompt` / `session.follow`），可选升级 HTTP + WebSocket。
- `session.follow(from_seq)` = **重放（日志）+ 订阅（实时）**。
- 客户端 `python -m harness attach`：流式渲染（rich）。

**验收**：两个终端 serve + attach 共享同一会话；kill attach 后重连能补齐缺失事件；协议 golden 测试；`docs/decisions/0009-follow-equals-replay-plus-subscribe.md`。

### M8 选修模块（每项 3–7 天，按兴趣）

| 模块 | 读 | 做 |
|---|---|---|
| 8.1 子代理与委派 | `dsh/packages/subagent/`（spawn/fork provider、tool-subagent） | `task` 工具：派生带独立会话的子代理，支持后续追问 |
| 8.2 上下文压缩 | `dsh/packages/compaction/` | 超预算时摘要旧区段；日志用"遮蔽"实现而非改写 |
| 8.3 审批与沙箱 | `dsh/packages/sandbox/`、`dsh/packages/interaction/permission-presets` | 权限预设（workspace-write / danger-full-access），fail-closed 审计 |
| 8.4 Skill 与 MCP | `dsh/packages/skill/`、`dsh/packages/mcp/` | 技能目录 + 加载工具；接入一个外部 MCP server |
| 8.5 Workflow / Ralph | `dsh/packages/workflow/` | 脚本化编排多个子代理完成一个目标 |

每项沿用同一循环：定验收 → 实现 → 测试 → 笔记。

---

## 5. 进度追踪

**P0 预热阶段（2026-09-30 完成）**：在正式进入 M1 前，先用单文件脚本把核心机制逐个跑通——
`P0_Coding/` 四个练习（模型调用、提示词拼装、工具调用闭环、文件工具沙箱与审批），15 个测试用例全绿。

**P1 阶段（2026-10-07 完成）**：把 P0 跑通的机制升级为结构化骨架，练习路线见 [P1_Coding/README.md](../P1_Coding/README.md)，
对应 M1–M3：`_01_Provider_Protocol` → `_02_Agent_Loop` → `_03_Tool_Pipeline` / `_04_Session_Log` → `_05_Mini_Harness`，
五个练习全部完成，P1 共 79 个测试用例全绿（真实 API 亦实测走通完整链路）。
`_05` 额外提供了一个**可视化页面**（标准库 http.server，零新增依赖）：把会话日志的事件流
实时搬到浏览器，日志轨迹随对话生长，崩溃修复/审批拒绝/分叉都能一键观察——让 M3 的
"事件溯源"从日志文件变成看得见的东西；页面直接调用真实 API（读取 `.env` 里的 key）。

**P2 阶段（进行中）**：把 P1 的产物收敛成单一 `harness/` 基线并补上门禁，再按「每阶段一个
最小扩展」拆成 `_01`–`_11` 十一个自包含目录，逐步做 M4–M7（提示词装配 → 能力接缝 →
组合与配置 → 服务化）。`_01`（section 注册表 + 装配器）、`_02`（变量插值 + 每 step 渲染、
`system/message` 进日志）、`_03`（装配单 + `--dump-prompt` + 重建断言）已完成——**M4 收官**；
`_04` 开始 M5：文件工具重构成 FileSystem 三角色接缝（LocalFS / MemoryFS 可互换），
`_05` 加策略型 provider `WorkspaceJailFS`（越界改动被拒，`FS_SANDBOX_DENIED`），
`_06` 给命令执行建第二条接缝 `SubprocessService`（LocalSubprocess / ScriptedSubprocess）
——**M5 收官**。`_07`–`_09` 完成 M6：顶层 `kernel/` 立下插件协议与 effect 台账
（`setup(ctx) -> disposer`；注册即 effect，卸载自动回卷）；`config/` + `profiles/*.yaml`
把装配变成配置数据（分层 patch，一层 CLI 补丁换 provider）；`dump-config` 让配置可见
（每项带来源层）且错误可定位（层/条/拼写建议）——**M6 收官**。
工作区与详细计划见 [P2_Coding/README.md](../P2_Coding/README.md)。

| 阶段 | 主题 | 状态 | 完成日期 | 验收命令 |
|---|---|---|---|---|
| P0 | 单文件预热练习 | ✅ 已完成 | 2026-09-30 | `pytest P0_Coding -q`（15 用例） |
| M0 | 工程地基 | ✅ 已完成（P2：`pyproject.toml` + `scripts/check.py`） | 2026-10-08 | `python scripts/check.py` |
| M1 | 最小 agent loop | ✅ 已完成（P1 `_01` + `_02`） | 2026-10-07 | `pytest P1_Coding -q` |
| M2 | 工具系统 | ✅ 已完成（P1 `_03`） | 2026-10-07 | `pytest P1_Coding -q` |
| M3 | 会话日志 | ✅ 已完成（P1 `_04` + `_05` 可视化） | 2026-10-07 | `pytest P1_Coding -q` |
| M4 | 提示词装配 | ✅ 已完成（P2 `_01` + `_02` + `_03`） | 2026-10-08 | `python chat.py --ask "..." --dump-prompt`（阶段入口；统一 CLI 待 M6 接线） |
| M5 | 能力接缝 | ✅ 已完成（P2 `_04` + `_05` + `_06`） | 2026-10-08 | `python chat.py --fs local\|jail\|memory --shell local\|fake` |
| M6 | 组合与配置 | ✅ 已完成（P2 `_07` + `_08` + `_09`） | 2026-10-09 | `python chat.py --profile dev --dump-config` |
| M7 | 服务化 | ☐ 未开始（P2 `_10`–`_11`） | | `python -m harness serve` + `attach` |
| M8 | 选修 | ☐ 未开始 | | 各模块自定义 |

每阶段结束更新此表；设计若变更，同步修改本文件对应阶段。
M1–M3 的产出落在 `P1_Coding/` 五个练习目录里（各练习自包含，组装见 `_05_Mini_Harness`）；
M4–M7 在 `P2_Coding/` 的十一个自包含阶段目录里继续（各带一份 `harness/` 副本；门禁 `scripts/check.py` 逐阶段运行）。
注意：P1 的决策记录已用掉编号 0001–0005，故 M4–M7 的决策记录从 **0006** 起编。

---

## 6. 如何读 dsh 参考代码

1. **先文档后代码**：每个包都有 `README.md`（目的/配置/扩展点），比源码快得多。
2. **服务定义 → 词汇表 → 实现**：常见顺序是 `src/index.ts`（服务类）→ `src/types.ts` → 具体 provider 与 consumer。
3. **测试是最好的文档**：`packages/**/tests` 里找行为测试，比读实现更快看清契约。
4. **"为什么"去 `.agents/notes/`**：dsh 每个非平凡改动都有决策笔记，按主题搜索。
5. **不要全读**：267 个包。只按本计划的"读"清单定点读，读完一个机制就写代码。
6. 读不懂时，拿具体文件路径向 AI 提问。

---

## 7. 环境与依赖

`.venv` 已就绪（Python 3.13）。安装：

```bash
.venv/Scripts/python.exe -m pip install openai pydantic pytest pytest-asyncio ruff mypy
```

| 阶段 | 新增依赖 |
|---|---|
| M0 | —（pytest / ruff / mypy 即可） |
| M1 | `openai`、`pydantic`、`pytest-asyncio` |
| M7 | `rich`；可选 `fastapi` + `uvicorn` + `websockets` |

环境变量：`DEEPSEEK_API_KEY`（写入项目根 `.env`，`.gitignore` 掉）。真实 API 只用于 demo；所有测试跑 FakeLLM。

---

## 8. 术语对照（dsh ↔ mini-harness）

| dsh | 本项目 | 含义 |
|---|---|---|
| turn / step | turn / step | 一次输入排空 / 一次模型请求 + 其工具调用 |
| capability seam | 接缝 | Definition + Provider + Consumer 三角色 |
| session log | 会话日志 | append-only 事件序列 |
| projection / deriveMessages | derive_messages | 从日志派生的视图（模型历史等） |
| bundle / profile | profile | 分层补丁组装 |
| Cordis ctx | Context | 插件共享的注册中心 |
| waterfall event | 管线 / 钩子链 | 逐层传递、可短路的中间件 |
| Agent Note | 决策记录 | `docs/decisions/` |
