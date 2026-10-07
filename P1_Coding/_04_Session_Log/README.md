# _04_Session_Log —— 会话日志：事件溯源（append-only + JSONL 落盘 + 投影）

P1 第四个练习，对应学习计划 **M3（全项目的心脏）**：把 _02/_03 那个"只活在内存里的
轨迹"升级成**事件溯源**的会话日志。核心思想一句话：

> **日志是唯一真相，模型历史只是它的投影。**

## 学习目标

1. 看清 **事件溯源**：会话不是"一个消息列表"，而是一条 append-only 的事件序列；
   模型历史由 `derive_messages()` 从事件投影出来，**绝不直接存储**。
2. 掌握 **落盘与崩溃恢复**：每条事件 append + flush 到 `session.jsonl`；进程被
   kill -9 留下的半截尾行，下次启动自动截断修复，**已确认事件不丢**。
3. 理解 **resume / fork**：同一个 `open_session(id)` 对新会话是"创建"、对旧会话
   是"恢复"，**同一条路径**；fork 用"重放日志前缀"派生新会话，原日志不动。
4. 记住 **同构断言**：重放日志得到的历史 == 在线执行产生历史。这是事件溯源
   正确性的验收标准，也是 M3 的核心测试。

## 文件（推荐阅读顺序）

| 顺序 | 文件 | 内容 | 对应 dsh |
|---|---|---|---|
| 1 | `session/events.py` | 事件模型：Event + 事件类型 + 写前校验 | `core/session/src/types.ts` |
| 2 | `session/projection.py` | `derive_messages`：日志 → 模型历史（纯函数） | `core/session/src/surface.ts` |
| 3 | `session/log.py` | `Session`：单调 seq、深拷冻结、append-only | `core/session/src/index.ts` |
| 4 | `session/store.py` | `JsonlStore`：落盘 / 崩溃尾部修复 / list | `session-persistence-jsonl/src/` |
| 5 | `session/recorder.py` | `SessionRecorder`：AgentLoop 事件 → 会话事件 | — |
| 6 | `runner.py` | 胶水：`open_session` / `Runner` / `fork_session` | — |
| 7 | `cli.py` | 会话列表 / 查看 / 继续 / 分叉 | `apps/cli` 的 sessions 子命令 |
| — | `agent_loop/`、`llm_seam/` | 自包含副本（agent_loop 有扩展，见下） | — |
| — | `demo.py` | 入口：离线全套演示（崩溃恢复 / 同构 / 分叉） | — |
| — | `test_session_log.py` | 13 个验收测试（全离线） | — |

> `agent_loop/` 相对 _02 有**三处扩展**（都在 `loop.py` 顶部注明）：支持
> `initial_messages` / `initial_turn`（resume 用）、事件负载增强（`step_response`
> 带完整 tool_calls、`tool_result` 带 call_id、补发 `step_end`），使事件流足以重建日志。

## 运行

```bash
cd P1_Coding/_04_Session_Log

# 全套演示（离线、不需要 key）：落盘 → 模拟崩溃 → 修复 → resume → 同构 → fork
python demo.py
python demo.py --keep            # 保留 sessions/ 演示目录

# CLI
python cli.py list
python cli.py show <session_id>
python cli.py run <session_id> "问题" --fake
python cli.py fork <src_id> <new_id> --upto 4

# 测试（全离线）
python -m pytest -q              # 13 个用例
```

## 验收标准（对应 M3）

- [x] **事件模型 + 校验**：8 类事件；未知类型 / 缺必填字段写入前 fail loud
- [x] **append-only + 单调 seq**：只能追加，seq 严格递增
- [x] **写入即冻结**：进日志深拷贝、读出来也深拷贝，外部改动污染不了日志
- [x] **投影**：`derive_messages` 正确映射 system/user/assistant/tool 并配对 tool_call_id
- [x] **落盘往返**：append → load 一致；`list_sessions` 可枚举
- [x] **崩溃尾部修复**：半行 JSON（无换行 / 有换行两种）都被自动截断，已确认事件不丢
- [x] **同构断言**：重放日志得到的历史 == 在线产生的历史 ★
- [x] **resume**：重开会话历史完整、turn 编号接续、system 只注入一次
- [x] **崩溃后 resume**：先修复坏尾，再继续对话，历史仍与重放一致
- [x] **fork**：拷贝到指定 seq 的前缀；原会话不动；目标已存在则报错
- [x] **system 提示进日志**：写成 `session/start`，投影出 system 消息
- [x] `pytest -q` 全部通过（13 个用例，0.09 秒）

## demo 实际输出（节选）

```
1) 新会话，跑一个 turn
   在线历史角色序列：['system', 'user', 'assistant', 'tool', 'assistant']
2) 会话日志（append-only 事件序列）
   # 1 session/start   # 2 turn/start   # 3 user/message   # 4 step/start
   # 5 assistant/message   # 6 tool/result   # 7 step/end   ...
3) 模拟崩溃：往 jsonl 尾部写半行 JSON
4) 恢复：repaired=True，丢弃 30 字节；恢复后事件数 11（半截事件已丢弃）
5) resume：历史完整、turn=2
6) 同构断言：✓ 一致（消息历史完全由日志投影而来）
7) fork：从第 4 条事件派生 demo-fork；原会话仍是 17 条
```

## 三个值得记住的设计点

1. **消息由日志派生，不是存储的**。`derive_messages` 是纯函数：同一份日志永远投影出
   同一份历史。这带来"重放必然同构"的性质——也是 resume/fork 能成立的根基。
2. **append + flush，每条都落盘**。不是攒批写；崩溃丢失窗口被压到最小。代价是
   每条事件一次 I/O（本练习可接受；真实系统会优化批写与 fsync 策略）。
3. **打开即恢复，没有专门的恢复分支**。`open_session` 对新建/已存在是同一段代码：
   装载日志 → 投影 → 构造循环。**恢复不是特例，而是常态路径**。

## 已知限制（故意的，后续主题解决）

- 单文件 JSONL，没有代际（generation）/ 压缩；日志增长没有上限；
- 没有写入加锁，多进程并发写同一会话会竞争（单会话单写者假设）；
- `fork` 是拷贝式，不是 dsh 的引用式分叉；`--upto` 之外没有更细的分叉粒度；
- 配合同名包自包含副本使用时，多练习全量测试需 `conftest.py` 驱逐同名模块
  （见下方"工程备注"）。

## 工程备注：同名包与全量测试

`_02/_03/_04` 各自带一份同名顶层包（`agent_loop` / `llm_seam` 等）。Python 的
`sys.modules` 按名字缓存，**全量跑（`pytest P0_Coding P1_Coding`）时**，先被导入的
那份会固化，导致别的练习 import 到错误版本。

解决：每个练习的 `conftest.py` 在该目录测试被导入前，**驱逐这些包及其全部子模块**
再把本目录置于 `sys.path` 最前。单跑某练习无副作用；`_05` 收敛成单一 `harness/`
包后，这个问题会从根上消失。
