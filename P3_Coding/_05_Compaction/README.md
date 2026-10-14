# _05_Compaction —— 上下文压缩（历史不改写，投影里折叠）

P3 第 5 阶段（对应 [PLAN.md](../PLAN.md)）。**本阶段只加一个机制**：

> 超阈值时把**旧区段**摘要成**一条消息**；日志用"折叠声明"实现，绝不改写历史。
> 验收：压缩后投影变化、日志原样、**重放仍然同构**。

**状态：✅ 已完成（2026-10-14）**

## 为什么这样设计

四个决定，每个都有明确的代价与理由：

**① 阈值：90% 触发、尾巴留 10%、摘要 ≤5% 容量**（用户定调）。
压完落在 15%~20%，离下一次触发很远——压缩必然打断一次前缀缓存，所以
**次数越少越好**。这是"少压一点、但压得很稀"的取向，与"频繁小压"相反。

**② 日志只追加一条 `compact/checkpoint`，不删不改任何旧事件。**
铁律 #1（模型可见即已记录）与"日志即真相"要求历史只能被**遮蔽/折叠**，
不能被重写。所以 checkpoint 是一句**声明**："写我的时候，对话列表最前面的
`folded` 条已经折成这句摘要了"。投影（`derive_messages`）读到时按**位置**
丢掉那几条、把摘要顶到最前——日志一条不动，重放自然同构。

> 踩坑记录：最初按 **seq** 折叠（"丢掉 seq ≤ X 的"）。但摘要自己带一个
> 更大的 seq 坐在列表最前，列表的 seq 因此不再单调——"折 1 条"实际掉了 3 条，
> 内存历史与日志投影当场失同步。**按位置折叠**与循环内存里的切片
> (`history[start : start+folded]`) 逐条对应，两个视图天然对齐。

**③ 当前 turn 永不折叠**（`protect_from` / `floor` 钳位）。
刚问的问题、刚拿到的工具结果不能在"干活干到一半"时被摘要掉。
代价写在"已知边界"里。

**④ 失败即放弃**（fail-soft）。摘要调用出错/返回空 → 只记一条瞬态
`compact_failed`（不进日志），本轮照常继续。压缩永远不能把对话搞死。

## 本阶段新增/改动

| 文件 | 角色 | 内容 |
|---|---|---|
| `components/agent/compact.py` | ★ **本阶段的机制** | `CompactionPolicy`（三个比例）/ `plan_compaction`（触发+切点，纯函数）/ `build_summary_request` / `Compactor` |
| `components/context/tokens.py` | 构件（新） | 字符→token 估算与构成拆分：从仪表插件提出来，供**仪表与压缩共用**；`DEFAULT_CONTEXT_WINDOW` 唯一默认值 |
| `components/session/events.py` | 词汇 | 新事件 `compact/checkpoint`（必填 `summary` + `folded`） |
| `components/session/projection.py` | 投影 | checkpoint 的**位置折叠**；`conversation_pairs` / `conversation_message_seqs`；越界（日志被改）当场报错 |
| `components/session/recorder.py` | 录制 | `compact_applied` → `compact/checkpoint`；`compact_failed` 列为瞬态 |
| `components/agent/loop.py` | 主循环 | 每 step **发请求前**检查；`turn_own` 计数保护当前 turn；fail-soft + `compact_applied` / `compact_failed` 事件 |
| `plugins/compact.py` | 插件 | 提供 `compacter` 服务（inject llm）；订阅循环事件 → 内核事件 `context/compacted` |
| `plugins/agent.py` | 接线 | 从 ctx 现取 `compacter` 交给每条会话的 loop；卸载 = 不再压缩 |
| `plugins/boot.py` / `chat.py` | 接线 | 默认装 compact；`--context-window` 默认 **1M**（`DEFAULT_CONTEXT_WINDOW`，用户指定） |
| `webui/index.html` | 界面 | 聊天流里的「⟳ 上下文已压缩」折叠徽标（可展开摘要全文）+ 事件流摘要行 + 失败横幅 |
| `plugins/context_meter.py` | 复用 | 估算/拆分改为引用构件（对外 API 不变，测试逐字兼容） |

## 怎么用

```bash
cd P3_Coding/_05_Compaction
python -m pytest -q                                # 231 个用例

python chat.py --web --realtime --self-tools        # 默认 1M 窗口
python chat.py --realtime --context-window 8000     # 小窗口：多聊几轮就能看见压缩
                                                    #（触发线 7200、尾巴 800、摘要 400）
```

界面上：聊天流里出现 `⟳ 上下文已压缩：折叠 N 条消息（约 X → Y tokens）`，
点开看模型眼前的摘要全文；事件流里同名事件带 `folded` / `before` / `after`。

## 机制怎么运作

```
每 step：              plan_compaction(内存历史, 工具面, 策略, measured=上次实测)
                          │
              用量 = max(字符估算, 实测 prompt_tokens) ── 不到阈值 → 什么都不做
                          │ 超了
                     choose_cut：从尾部往回攒到"保留预算"
                          ├─ floor 钳位：当前 turn 一条都不折
                          ├─ min_tail：末尾至少 2 条（一次工具往返）
                          └─ 成对安全：切点落在 tool 消息上就左退
                          │
                    一次普通模型调用（无工具、历史摊平成文本）
                          │  失败/空 → compact_failed（瞬态，本轮跳过）
                    折叠：history[start : start+folded] → [一条摘要消息]
                          │
                    emit compact_applied ──► 录制器追加 compact/checkpoint
```

**触发用量的两个来源**：估算（无 tokenizer）与实测（`usage.prompt_tokens`）。
取 `max` 是保守的一侧——实测来自**上一个**请求，本请求只会更大。
`used_source` 如实标注是哪一个（页面上也能看到）。

**摘要请求的内容**：折叠区消息**摊平成文本**（`[user] … [tool] 结果 → …`），
不原样发——因为很多供应商要求"带 tool 消息就必须同时带 tools 声明"，
摊平谁都能读。超长时"第一条 + 从最新往回填"，中间标"已略"（裁剪有损，
但压缩本身有损，写明比假装完整强）。

## 实测数据

端到端（离线，容量 3000 逼出压缩，8 轮）：

```
turn 1: S u a                       （S=system）
turn 2: S u a u a
turn 3: S u u a        ← 第 3 轮触发首次压缩
turn 4: S u a u a      ← 注意：摘要以 user 身份顶在最前（"u u"里的第一个）
...
压缩次数: 3
  seq=16 turn=3 folded=4   3152 → 717 tokens
  seq=29 turn=5 folded=5   3286 → 717
  seq=42 turn=7 folded=5   3286 → 717
user/message 条数（应=8，日志未删）: 8
```

真实 API（小窗口）见"真实 API 验证"一节。

## 验收（对照 PLAN.md）

- [x] **压缩后投影变化**：折叠区变成一条摘要消息（`【早前对话摘要】`）
- [x] **日志原样**：`user/message` / `assistant/message` / `tool/result` 一条没少、
      内容没改、seq 严格递增；被折掉的原文**仍在日志里**
- [x] **重放仍然同构**：重新装载日志（新进程视角）投影出的历史，与在线时的
      内存历史**逐条相等**（含工具配对与摘要）
- [x] 第二次压缩把第一版摘要一起吞掉（摘要不链式堆积）
- [x] 成对安全：切点不落在 `tool` 消息上；跨切点的配对两边同在折叠区或同在保留区
- [x] 当前 turn 永不折叠（`floor` 钳位；整个历史都属于本 turn 时一条都不折）
- [x] fail-soft：摘要失败 → 对话照常 `done`、日志里没有 checkpoint、下次还能压
- [x] 压缩触发时发内核事件 `context/compacted`（界面可见）
- [x] 卸载 compact 插件 = 新会话完全不压缩（`loop._compactor is None`）
- [x] 门禁绿（ruff + pytest，231 用例）

## 已知边界（诚实记录）

1. **单个 turn 内疯长压不动**：几百次工具往返把上下文撑爆时，压缩区够不到
   当前 turn（折掉正在干的事会让模型当场失忆）。这类"单个巨大工具结果"是
   `_07`（大结果搬运）的题目。
2. **压缩必然打断一次前缀缓存**：摘要进请求意味着前缀变了，下一次调用全 miss。
   这正是"90% 才压、压完落在 15%~20%"的价值——把打断次数压到最低。
3. **摘要有损**：所以尾巴留 10% 原版；且摘要被要求保留"目标/决定/路径/待办"四类。
4. **估算不是 tokenizer**：中文/其他按 0.6/0.3 估算，触发判断有误差；
   实测一侧（`prompt_tokens`）在真实 provider 下会持续纠正它。

## 完成后的去向

`_06` 起按垂类裁剪（见 PLAN.md）。`_05` 完成是**整合包重生成的时机之一**
（基础四件套齐：流式 / 用量 / 仪表 / 压缩），`mini_harness/` 基线会在
`_05` 收尾时同步再生。

---

> 目录说明：`_05` 是开发位置（从 `_04` 完成态复制）；`mini_harness/` 是基线快照。
> 阶段定位与路线见 [PLAN.md](../PLAN.md) 与 [../README.md](../README.md)。
