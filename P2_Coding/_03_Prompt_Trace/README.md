# _03_Prompt_Trace —— 装配来源追溯与重建断言

P2 第 3 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> `--dump-prompt` 打印每个 section 的来源；并断言「渲染出的提示词能由 日志 + 装配器 重建」。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0008](../../docs/decisions/0008-prompt-trace.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_02` 完成后的
`harness/` + `prompt/` + `context/`，本阶段给 `prompt/` 加装配单、给 `context/` 加追溯读取）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增/升级的机制全部在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `prompt/`（顶层） | `_01`/`_02` 的机制 + 本阶段升级 | ★ 新增 `trace.py` 装配单；装配器加 `separator` 只读属性 |
| `context/`（顶层） | `_02` 的机制 + 本阶段升级 | ★ 事件携带装配单；新增 `latest_prompt_trace` 追溯读取 |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |
| `chat.py` / `demo.py` / `tests/` | 入口与测试 | `--dump-prompt` / 第 0 节演示装配单与重建 |

## 0) 现状基线

```bash
cd P2_Coding/_03_Prompt_Trace
python -m pytest -q        # 22 个基线用例 + 27 个本阶段用例 = 49
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `docs/subsystems/system-prompt.md`（可追溯性一节）

**做**：
- ✅ `--dump-prompt`：打印装配顺序、每个 section 的内容与**来源**、引用变量与取值；
- ✅ 重建断言：`日志 + 装配器 == 当时渲染出的提示词`（`rebuild_text(装配单) == 记录文本`）。

**验收**：
- [x] dump-prompt 可读、含来源信息（name / title / source / 引用变量 / 模板 vs 渲染）
- [x] 重建断言通过：每 step「模型收到的文本 == 由该步日志重建的文本」
- [x] 检验有牙齿：篡改记录 → 重建对不上；抹掉变量 → fail loud
- [x] 快照稳定；基线 22 用例仍全绿；门禁绿（49 用例，ruff 通过）

## 2) 本阶段新增的东西

**`prompt/trace.py` —— 装配单（PromptTrace）**：

| 概念 | 内容 |
|---|---|
| `SectionTrace` | 一格配料：name / source / title / 模板 / 渲染文本 / 引用变量 |
| `PromptTrace` | 一整份装配单：各节 + 变量取值 + 分隔符 + 最终文本；可 `to_dict`/`from_dict` |
| `capture_trace(assembler, variables)` | 渲染一次并生成装配单（与 `assemble()` 同一套渲染，不会分叉） |
| `rebuild_text(装配单或字典)` | **由记录重建**：新建注册表 → 注册模板 → 用同一装配器拼接 |

**`context/` 的两处升级**：

- `session/start` 与 `system/message` 事件现在携带 `prompt_trace` 字段——日志里存的不再只是
  一段文本，还有"它是怎么拼出来的"；
- `latest_prompt_trace(events)`：当前生效的装配单（与 `effective_system_prompt` 同一条遮蔽链）。

**为什么"重建"有牙齿**：重建**不碰进程里的原注册表/作用域**，只用记录里的数据重新装配——
记录缺变量会 fail loud，记录被改动过就重建不出原文。于是"可由日志重建"不是一句口号，
而是一条每次渲染都在被验证的不变量（测试与 demo 都有"篡改检测"用例）。

## 3) 运行方法

```bash
cd P2_Coding/_03_Prompt_Trace
python -m pytest -q

# ★ 交互入口：--dump-prompt 打印装配单（来源 / 变量 / 重建校验）
python chat.py                                   # 真实 API（读取项目根 .env 的 key）
python chat.py --fake --ask "现在几点？"           # 离线跑一个 turn
python chat.py --fake --ask "现在几点？" --dump-prompt   # 附带装配单与重建校验
python chat.py --session s1 --dump-prompt         # 恢复旧会话看它的装配单
# 会话内命令：/prompt 当前生效装配单；/context 生效文本；/history；/exit

# 脚本化演示（离线，跑完整故事线）
python demo.py        # 第 0b 节：装配单 / 序列化往返 / 重建 / 篡改检测 / 缺变量 fail loud
                      # 第 0c 节：每 step 渲染 —— 装配单随事件进日志，逐步可重建
./run.bat             # 双击即离线 demo；run.bat chat --fake --dump-prompt 装配单对话
```

**真实对话里能看到什么**：`--dump-prompt` 启动即打印开场装配单（每节的来源、引用变量、
模板与渲染对比），每次提示词因时间变化而更新时再打印一份新装配单，末尾都带重建校验；
`/prompt` 随时看"当前生效的装配单"（取自日志，不是重新渲染）——追溯的是"当时"而不是"现在"。

## 4) 完成后的去向

**M4 完成**（提示词可组合 ✅ / 动态化 ✅ / 可追溯可重建 ✅）。
下一阶段 `_04_Filesystem_Seam` 进入 **M5 能力接缝**：把文件工具重构成
Definition/Provider/Consumer 三角色，为"换 provider 换产品"铺路。
