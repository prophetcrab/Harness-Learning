# _06_Goal —— 持久目标（一个会话一个目标，状态由日志派生）

P3 第 6 阶段（对应 [PLAN.md](../PLAN.md)）。**本阶段只加一个机制**：

> 一个**跨轮次、跨重启**的持久目标。模型能建/改/暂停/完成/标记卡住，
> 人能用 `/goal` 直接管（不花模型的钱）；目标处于 active 时会**自动续跑**，
> 由轮次上限兜底防跑飞。**状态由日志投影派生，不另存一份。**

**状态：✅ 已完成（2026-10-14）**

## 为什么这样做

**① 状态是"投影"，不是"文件"。** 每次变更往会话日志追加一条 `goal/change`
（带变更后的完整快照），当前目标由 `fold_goal()` 重放出来。于是"跨 turn 持久"
"跨进程重启""fork 后仍在"三件事**一次都不用额外做**——它们本来就是"日志是
唯一真相"的免费结果（P1 `_04` 立下的规矩，这里是它的又一次兑现）。

**② 一个会话一个目标，且是"完成条件"不是"待办清单"。**
目标回答"这件事做完是什么样"，所以它需要跨很多轮才值得建；日常单轮的事不该建
（这条写进工具描述，靠模型判断）。同时只允许一个未完成目标——想换目标先
pause / complete / clear，免得两个目标互相打架。

**③ 三道闸门防跑飞（这是本阶段最要紧的地方）。**
active 的目标会自动续跑，没有护栏就是"一个想不明白的目标烧掉一整天"：

| 闸门 | 作用 |
|---|---|
| **轮次上限**（默认 8） | 跑满就 `blocked`（原因码 `rounds-exhausted`）并写明为什么停 |
| **取消即停** | `cancel` / `stop` 后本轮收尾即止，不再续 |
| **只在 active 续** | paused / blocked / complete / 无目标都不续 |

**④ 版本号（CAS）挡住"拿着旧视图改"。** 每次变更 revision+1，改的时候要带上
你看到的版本；对不上就拒绝并提示重新读。模型很容易在长对话里拿着一小时前的
目标视图来改——这条把它拦在写坏数据之前。

**⑤ 人命令不花模型的钱。** `/goal` 直接操作服务（对齐 dsh 的 `command-goal`）：
看一眼、建、改、暂停、完成、清掉，都是本地操作。命令的**解析与排版在构件层**
（`components/goal/command.py`），网页与 REPL 共用同一份——两个入口各写一遍
解析必然漂移（一个支持 clear 另一个忘了）。

## 本阶段新增/改动

| 文件 | 角色 | 内容 |
|---|---|---|
| `components/goal/domain.py` | ★ **本阶段的机制（纯逻辑）** | 类型（`GoalSnapshot`/`GoalProjection`）、许可表 `ALLOWED_TRANSITIONS`、`next_snapshot`（变更后的样子）、`fold_goal`（重放折叠）、CAS `check_revision` |
| `components/goal/store.py` | 服务面 | `GoalStore`：语义动作（create/edit/pause/…）→ 校验 → 追加 `goal/change` |
| `components/goal/command.py` | 人命令 | `/goal` 的解析 + 排版（`run_goal_command`，**永不抛异常**，打错回用法） |
| `components/session/events.py` | 词汇 | 新事件 `goal/change`；`user/message` 增加可选 `source` 字段 |
| `components/agent/loop.py` | 循环 | `run(..., source=)` 把"谁说的"带进 `turn_start` |
| `components/session/recorder.py` | 录制 | `source` 落进 `user/message`（`human` 缺省不写，老日志原样可读） |
| `plugins/goal.py` | 插件 | `goals` 服务 + 三件模型工具 + 续跑闸门 `GoalContinuation` |
| `plugins/agent.py` | 接线 | 一轮跑完之后问闸门；该续就再跑一轮（两个入口都自动获得续跑） |
| `plugins/sessions.py` | **修 bug** | 同名会话复用同一个 `Session` 实例（见下文"踩到的坑"） |
| `plugins/webui.py` + `webui/app.js` | 界面 | 命令菜单加 `/goal`（带子参数）；`GET/POST /api/sessions/<id>/goal` |
| `chat.py` | 入口 | REPL `/goal` 命令 |

## 怎么用

```bash
cd P3_Coding/_06_Goal
python -m pytest -q

# REPL
python chat.py
你：/goal new 把文档里所有失败的检查项修好 --rounds 6
你：/goal                        # 看一眼进度
你：/goal pause                  # 先别自动跑了

# 网页：输入框敲 / → 菜单里选 /goal → 补全成 "/goal " → 接着打子命令
```

模型侧三件工具：`get_goal` / `create_goal` / `update_goal`
（`update_goal` 的 action：`edit` / `pause` / `resume` / `complete` / `block` / `clear`）。

## 机制怎么运作

```
模型建目标 → create_goal 工具
     │  校验（许可 + CAS + 负载）→ 追加 goal/change（含完整快照）
     ▼
日志 = 唯一真相 ──fold_goal()──► 当前目标（投影）
     │
人或模型改它 ──► 又一条 goal/change（revision+1）
     │
一轮人消息跑完 ──► 续跑闸门问三句：
     ① 被取消了吗？ ② 还在 active 吗？ ③ 轮次还有吗？
        ├─ 都过 → 自动再跑一轮（user/message 带 source="goal-round"）
        └─ 轮次用尽 → 标 blocked（rounds-exhausted）并说明
```

**为什么续跑放在 `AgentService.ask` 而不是循环里**：循环只认识"一个 turn"，
"要不要再开一个 turn"是**会话级**决策，而会话归 `AgentService` 管。放这一层，
两个入口（REPL / 网页）都自动获得续跑，一处都不用改。

## 验收（对照 PLAN.md）

- [x] **跨 turn 持久**：目标在多个 turn 之间存活；续跑轮次记在投影里
- [x] **投影一致**：`fold_goal` 是纯函数（同一份日志折两次结果相同）；
      坏日志（乱序 revision、blocked 无原因、id 复用）**当场报错**，
      不静默折出一个假目标
- [x] **resume 后还能看到**：重新装配打开同一会话，目标逐字相同且可继续改
- [x] 一个会话一个未完成目标；完成的目标可直接替换；id 不回收
- [x] 许可表逐格验证（8 组阶段×操作）
- [x] CAS：过期版本号被拒并提示重新读
- [x] **三道闸门**：轮次用尽 → blocked 且停（`1 + 3 = 4` 个 turn，不多不少）；
      暂停不续；被卡不续；取消不续
- [x] 人命令全流程 + 中文别名 + **打错不炸**（回用法）
- [x] 卸载插件：服务、工具、闸门一起回卷
- [x] 不装 goal 插件时行为与 `_05` 完全一致（一轮就结束）

## 踩到的坑（两个真 bug）

**① 同名会话有两个 `Session` 对象。** `SessionsService.open()` 起初每次新建
实例，于是 agent 和 goal 存储各拿一份**内存副本**：一方 append 的事件，另一方
在自己的 `_events` 列表里永远看不到（磁盘上其实写了），结果"目标轮次"数成 0、
`goal/change` 折不出来。修法是按 session_id 缓存实例——**日志是唯一真相，
真相的句柄也应当唯一**。（这是 `_06` 逼出来的既有隐患，`_05` 之前没人同时读。）

**② 模型工具写死了会话。** 工具面是**全局注册**的（不区分会话），初版把目标
写死进 `"default"`，于是在 `g1` 里建的目标跑进了 `default`——`view("g1")` 永远
是空的。修法是"现取当前会话"：`ask` 持锁时把会话名告诉目标服务（与 `_03`
用量账本记 session 的做法一致）。

## 已知边界（诚实记录）

1. **续跑是"跑完一轮再判断"，不是并行调度**：一轮很长（多步工具）时，
   暂停要等这一轮结束才生效。
2. **目标不调度工作**：它只记"要完成什么"，不保证有轮次预算就一定推进——
   dsh 也是这么分的（续跑是独立包）。
3. **`blocked` 之后不会自己好**：要人或模型显式 `resume`。
4. 续跑的提示词是固定的那一句（`CONTINUE_PROMPT`）：背景全在目标与历史里，
   不另做摘要注入。

## 完成后的去向

`_07_Spill`（大输出转存）——`_05` 的记录里已经点名它是"单个巨大工具结果"的正解，
而目标续跑会让会话更容易跑长，转存的需求更实在。`_06` 起按垂类裁剪，
PLAN 里 `_07` 之后各阶段都标着"依赖垂类"。

---

> 目录说明：`_06` 是开发位置（从 `_05` 完成态复制）；`mini_harness/` 是基线快照。
> 阶段定位与路线见 [PLAN.md](../PLAN.md) 与 [../README.md](../README.md)。
