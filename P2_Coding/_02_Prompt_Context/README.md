# _02_Prompt_Context —— 变量插值与运行时上下文

P2 第 2 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> section 里的 `{{cwd}}`/`{{platform}}`/`{{time}}` 在**每 step 渲染**时求值，渲染结果作为 `system/message` 事件进日志。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0007](../../docs/decisions/0007-prompt-context.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_01` 完成后的
`harness/` + `prompt/`，本阶段把 `prompt/` 升级并新增 `context/`）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增/升级的机制全部在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `prompt/`（顶层） | `_01` 的机制 + 本阶段升级 | ★ 新增 `interpolate.py` 插值；装配器 `assemble(variables)` |
| `context/`（顶层） | ★ **本阶段新增的机制** | 运行时上下文 / 渲染器 / provider 包装 / 会话扩展 / 接线 |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |
| `chat.py` / `demo.py` / `tests/` | 入口与测试 | 接线点：`open_context_harness(...)` |

## 0) 现状基线

```bash
cd P2_Coding/_02_Prompt_Context
python -m pytest -q        # 22 个基线用例 + 35 个本阶段用例 = 57
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/context/`（time-context 等的"每 step 注入上下文"机制）
- `core/system-prompt` 的运行时渲染部分

**做**：
- ✅ 变量插值 `{{cwd}}` / `{{platform}}` / `{{time}}`（未知变量 fail loud）；
- ✅ 每 step 渲染运行时上下文，渲染有变化时作为 `system/message` 事件写进会话日志；
- ✅ 投影扩展：最近一次 `system/message` 遮蔽早先文本（日志只增不改）。

**验收**：
- [x] 三种变量的插值正确（含 `{{ x }}` 空白容错、未知/非法变量 fail loud）
- [x] 改 cwd 只影响引用它的 section，其它 section 逐字节不变
- [x] `system/message` 进日志且可投影（投影里只有一条 system，为最近一次渲染）
- [x] 快照稳定：上下文不变 → 日志不产生多余事件；变化才记录
- [x] 模型每步实际收到的 system 文本 == 由日志（`system/start` + `system/message` 遮蔽）重建的文本
- [x] 基线 22 用例仍全绿；门禁绿（57 用例，ruff 通过）

## 2) 本阶段新增的东西

**顶层 `prompt/` 的升级**（纯逻辑，不依赖 harness）：

| 文件 | 内容 |
|---|---|
| `interpolate.py` | `render_text`：`{{name}}` 最小插值（空白容错；未知/非法/非字符串值 fail loud）；`referenced_names` 供追溯 |
| `assembler.py` | `assemble(variables)`：拼接时插值；模板与渲染结果分离（`parts()` 给模板，`assemble()` 给文本） |
| `defaults.py` | 默认四节：role / tools / **env**（本阶段新增，引用三个变量）/ style |

**顶层 `context/` 包**（本阶段的核心机制）：

| 文件 | 内容 |
|---|---|
| `runtime.py` | `RuntimeContext`（不可变快照）+ `collect_runtime_context(workspace, now=…, platform_name=…)` |
| `renderer.py` | `PromptRenderer`：装配器 + 上下文来源 → **可反复调用**的渲染器（每次 `render()` 重新采样） |
| `provider.py` | `RuntimePromptProvider`：provider 包装——每 step 渲染/记录/改写请求（挂钩点在 M1 接缝上，循环不用改） |
| `session.py` | `ContextSession`：扩展事件词汇（`system/message`）+ 遮蔽投影 |
| `projection.py` | `project(events)` / `effective_system_prompt(events)`：最近一次 `system/message` 生效 |
| `wiring.py` | `open_context_harness(...)`：把以上接成一条会话（替代 `MiniHarness.open` 供本阶段入口使用） |

**关键语义**（三条）：

1. **每 step 渲染**：`AgentLoop` 每个 step 恰好调一次 `provider.complete()`——包装器在这里
   重新采样上下文、重新渲染，并用最新文本替换请求里的 system 消息。模型每一步看到的
   都是"当时"的 `{{time}}`。
2. **变化才记录**：渲染结果与日志里"当前生效文本"相同 → 不产生事件；不同 → 追加一条
   `system/message`。于是"每步看到的文本"仍可由日志重建（最近一次记录持续生效），
   而日志不会被不变的重复渲染灌水。resume 时以日志里的生效文本为基线，重开不重复记。
3. **开场快照**：新会话把"会话开始那一刻的渲染"写进 `session/start`（第 0 步）；
   此后每一步若变化，追加 `system/message` 遮蔽它——日志只增不改（铁律 #2）。

## 3) 运行方法

```bash
cd P2_Coding/_02_Prompt_Context
python -m pytest -q

# ★ 交互入口：每 step 渲染系统提示词的真实对话
python chat.py                      # 真实 DeepSeek API（读取项目根 .env 的 key）
python chat.py --fake               # 离线剧本对话（不需要 key）
python chat.py --ask "现在几点？"    # 一条问题跑一个 turn 后退出
python chat.py --session s1         # 指定会话；同名即"恢复继续"
python chat.py --no-scope           # 只看基础四节，不叠加 chat 作用域
# 会话内命令：/prompt 现场重新渲染（时间按此刻取值）；/context 当前生效文本；/exit 退出

# 脚本化演示（离线，跑完整故事线）
python demo.py        # 第 0a 节：插值机械演示（改 cwd 只动 env 一节；未知变量 fail loud）
                      # 第 0b 节：脚本时钟驱动"每 step 渲染"→ system/message 进日志 → 可重建断言
./run.bat             # 双击即离线 demo；run.bat chat --fake 离线对话
```

**真实对话里能看到什么**：`chat.py` 每个 step 都会重新渲染——启动时打印模板与当下渲染；
每轮回复后若提示词发生了变化（时间走过一秒就会变），会提示"系统提示词更新 N 次"；
`/prompt` 现场重新渲染给你看时间在走；`/context` 显示日志里当前生效的那份。
会话日志里可以看到 `session/start` → 若干 `system/message` 的演进轨迹。

## 4) 完成后的去向

`_03_Prompt_Trace` 加「来源追溯」与「可由日志 + 装配器重建」的断言：
`--dump-prompt` 打印每节的 name / source / 变量引用，并断言"日志里的提示词 == 用日志
记录的结构重新装配的结果"（本阶段的 `referenced_names` 与 `parts()` 已为此留好接口）。
