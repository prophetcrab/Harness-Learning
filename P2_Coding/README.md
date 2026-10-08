# P2_Coding

## P2 简介

P2 是"把 harness 长成一个真正的产品"的阶段。它延续 P1 的思路——**每个阶段 = 一个最小扩展**——
把 M4–M7 的机制拆成 **11 个可独立验收的小阶段**。每个阶段只加一个机制，且能在上一阶段
`harness/` 的基础上单独跑通。

对应学习计划：**M4（提示词装配）→ M5（能力接缝）→ M6（组合与配置）→ M7（服务化）**。

| 编号 | 机制（本阶段唯一新增的东西） | 计划阶段 |
|---|---|---|
| `_01_Prompt_Sections` | 提示词 section 注册表 + 装配器 | M4 |
| `_02_Prompt_Context` | 变量插值 + 运行时上下文（每 step 渲染、进日志） | M4 |
| `_03_Prompt_Trace` | `--dump-prompt` 来源追溯 + "可由日志重建"断言 | M4 |
| `_04_Filesystem_Seam` | `FileSystem` 接缝 + LocalFS / MemoryFS | M5 |
| `_05_Workspace_Jail` | `WorkspaceJailFS` 策略 provider（越界写被拒） | M5 |
| `_06_Subprocess_Seam` | `SubprocessService` 接缝 + `shell` 工具 | M5 |
| `_07_Plugin_Effect` | 插件协议 `setup(ctx)->disposer`，注册即 effect | M6 |
| `_08_Profile_Layers` | `profiles/*.yaml` 分层 patch，一行换 provider | M6 |
| `_09_Dump_Config` | `dump-config` + 配置错误定位 | M6 |
| `_10_Rpc_Transport` | stdio JSON-RPC：`initialize` / `session.prompt` | M7 |
| `_11_Session_Follow` | `session.follow`（重放+订阅）+ `attach`（断线补齐） | M7 |

依赖关系：`_01 → _02 → … → _11`。**后一阶段从上一阶段的 `harness/` 复制起点。**

**沿用 P1 的约定**：每阶段自包含、四件套（入口 + README + 测试 + 一键启动器）、
先定验收再实现、测试走 `FakeLLM` 离线断言、真实 API 只用于 demo。
**与 P1 的差异**：阶段之间**不共享代码**——每个 `_0N` 目录自带一份完整 `harness/` 副本。

> 为什么拆成 11 个而不是 4 个（M4–M7 各一个）？因为 P1 的经验是：**一个阶段只加一个机制**
> 时，教学边界和验收标准都最清晰。M4 的"装配器"和"追溯"、M5 的"接缝"和"策略 provider"、
> M6 的"插件 effect"和"配置分层"、M7 的"传输"和"跟随"——每一对都值得各自独立验收。

## 目录

```
P2_Coding/
├── README.md            ← 本文件
├── pyproject.toml       ← ruff 配置（阶段式工作区，不做 setuptools 打包）
├── scripts/check.py     ← 门禁：ruff（整体）+ pytest（逐阶段）
└── _01.._11_<主题>/     ← 11 个自包含阶段（见上表）
```

每个 `_0N` 目录结构一致（**新机制放阶段主目录顶层，与 `harness/` 平级**）：

```
_0N_<主题>/
├── README.md          ← 本阶段学习计划（读 dsh / 做 / 验收 / 运行 / 与基线的接口）
├── harness/           ← M1–M3 基线库（各阶段自带的副本；本阶段不动它）
├── <新模块>/          ← ★ 本阶段新增的机制（顶层模块，如 _01 的 prompt/）
├── tests/             ← 22 个基线验收测试 + 本阶段机制用例
├── demo.py            ← 离线端到端回归 + 本阶段机制的演示
├── conftest.py        ← pytest 引导（隔离本阶段的同名顶层包）
└── run.bat / run.sh   ← 一键启动器
```

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**，各阶段共享、不改动；
> 各阶段**新增的机制放在阶段主目录的顶层**，与 `harness/` 平级。这样"这一阶段加了什么"
> 在目录层面一目了然，前四个阶段的基线也能原样复制给下一阶段。

**当前状态**：11 个阶段都是**同一份基线代码**（`harness/`，P1 `_05` 语义等价的单一包）；
`_01`–`_03` 完成 **M4 提示词装配**（section 注册表 → 插值/每 step 渲染 → 装配单/重建）；
`_04` 开始 **M5 能力接缝**：顶层 `providers/` 把文件工具重构成三角色接缝
（FileSystem 定义 + LocalFS/MemoryFS + 接缝工具），`chat.py --fs local|memory` 换实现；
其余阶段的 M5–M7 实现尚未落地。

---

## 模块简介与使用方法

每个阶段都能单独跑。**通用命令**（在对应阶段目录下）：

```bash
python -m pytest -q                # 基线验收测试（22 用例，全离线）
python demo.py                     # 离线端到端回归（对话 → 工具 → 退出 → resume → 分叉）
python -m harness list             # CLI
python -m harness.webui.server     # 可视化页面（真实 API，http://127.0.0.1:8765/）
./run.bat                          # 双击即离线 demo；run.bat chat --fake 交互对话
```

各阶段的**招牌命令**（`【将实现】` 标注的是本阶段要新增、当前尚不存在的能力）：

| 阶段 | 招牌命令 |
|---|---|
| `_01_Prompt_Sections` | `python -m harness run "帮我算 1234*56.78" --fake` |
| `_02_Prompt_Context` | `python chat.py --fake --ask "现在几点？"`（每 step 渲染 `{{time}}`） |
| `_03_Prompt_Trace` | `python chat.py --fake --ask "现在几点？" --dump-prompt`（装配单 + 重建校验） |
| `_04_Filesystem_Seam` | `python chat.py --fs memory --ask "把 hello 写到 notes/a.txt"`（换 provider 换文件世界） |
| `_05_Workspace_Jail` | `python -m harness run "把 x 写到 ../escape.txt" --fake`（应被拒） |
| `_06_Subprocess_Seam` | `python -m harness run "用 shell 看看当前目录" --fake` 【将实现】 |
| `_07_Plugin_Effect` | 卸载插件后注册物自动回卷（见该阶段测试）【将实现】 |
| `_08_Profile_Layers` | `python -m harness --profile dev run "帮我算 2+3"` 【将实现】 |
| `_09_Dump_Config` | `python -m harness --profile dev dump-config` 【将实现】 |
| `_10_Rpc_Transport` | `python -m harness serve`（stdio JSON-RPC）【将实现】 |
| `_11_Session_Follow` | 两终端 `serve` + `attach`，kill 后重连补齐事件 【将实现】 |

### 门禁（在 P2_Coding 目录下）

```bash
python scripts/check.py        # ruff（整体）+ pytest（逐阶段，11 个）
python scripts/check.py --fix  # 先让 ruff 自动修可修的问题
```

---

## 更新规则（每完成一个阶段）

1. 在该阶段目录里写实现 + 测试，**跑通该阶段 README 的验收清单**；
2. 更新该阶段 README 的验收勾选与本篇进度表；
3. 写决策记录 `docs/decisions/000N-*.md`（见各阶段 README 顶部；0001–0005 已被 P1 占用，
   故从 0006 起编）；
4. `python scripts/check.py` 全绿；
5. 下一个阶段的起点 = 复制本阶段完成后的 `harness/` 目录。

## 进度

| 阶段 | 机制 | 状态 | 完成日期 |
|---|---|---|---|
| `_01_Prompt_Sections` | section 注册表 + 装配器 | ✅ 已完成 | 2026-10-08 |
| `_02_Prompt_Context` | 变量插值 + 运行时上下文 | ✅ 已完成 | 2026-10-08 |
| `_03_Prompt_Trace` | `--dump-prompt` + 重建断言 | ✅ 已完成 | 2026-10-08 |
| `_04_Filesystem_Seam` | FileSystem 接缝 + Local/Memory | ✅ 已完成 | 2026-10-08 |
| `_05_Workspace_Jail` | WorkspaceJailFS 策略 | ☐ 未开始 | |
| `_06_Subprocess_Seam` | Subprocess 接缝 + shell | ☐ 未开始 | |
| `_07_Plugin_Effect` | 插件协议 + effect 回卷 | ☐ 未开始 | |
| `_08_Profile_Layers` | YAML 分层 patch | ☐ 未开始 | |
| `_09_Dump_Config` | dump-config + 错误定位 | ☐ 未开始 | |
| `_10_Rpc_Transport` | stdio JSON-RPC | ☐ 未开始 | |
| `_11_Session_Follow` | follow + attach | ☐ 未开始 | |

> **阶段名 vs 计划阶段**：目录名是**机制主题**（一次最小扩展），它服务的**学习计划阶段**
> 是 M4–M7，见各阶段 README 顶部标注。
