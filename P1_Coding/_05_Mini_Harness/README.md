# _05_Mini_Harness —— 综合验收：把前四个练习组装成一个可用的小 harness

P1 最后一个练习，对应学习计划 **M1–M3 的组装验收**：前四个练习各自把一块机制做扎实
（接缝 / 主循环 / 工具管线 / 会话日志），本练习把它们**接成一个能对话、能恢复、
可扩展工具、可离线测试**的最小 harness——并且**没有为组装新增任何机制**。

## 学习目标

1. 体验**组装视图**：一个 harness 是"若干接缝对接"的产物。本练习几乎只写粘合代码
   （`MiniHarness` + `app.py`），核心逻辑全部复用前四个练习。
2. 验证**接缝确实能对接**：
   - `ToolPipeline.execute()` 满足 `AgentLoop` 的执行器契约 → 管线插进循环，循环零改动；
   - `SessionRecorder` 作为 `AgentLoop` 的 `on_event` 回调 → 事件流落进会话日志；
   - `Session.derive_messages()` 产出循环的初始历史 → **resume 就是把日志投影喂回循环**。
3. 走通**完整产品路径**：多轮对话 → 工具（算数/读写文件）→ 审批 → 退出 → 重新进入
   → 历史完整、turn 接续、工作区文件仍在 → 同构断言 → 分叉。
4. 体会**组装层的取舍**：只做"接线 + 工具面 + 入口（CLI / 可视化）"，不引入新抽象
   （理由见决策记录 0005）。
5. 用**可视化页面**把"事件流"从日志文件变成一个能看见的东西：日志轨迹随对话实时生长，
   崩溃修复、审批拒绝、分叉都能一键观察。

## 文件（推荐阅读顺序）

| 顺序 | 文件 | 内容 |
|---|---|---|
| 1 | `mini_harness.py` | ★ 装配核心：`MiniHarness`（provider + 管线 + 循环 + 日志） |
| 2 | `workspace_tools.py` | 默认工具面：calculate / read_file / write_file / list_files（沙箱 + 审批） |
| 3 | `web_search.py` | 可选工具：Bing 搜索（--search 才注册，保持默认离线） |
| 4 | `env.py` | 环境与 provider 构造（唯一的"选供应商"落点） |
| 5 | `app.py` | CLI：`chat` / `run` / `list` / `show` / `fork` |
| 6 | `server.py` | ★ 可视化服务：stdlib http.server + JSON/NDJSON API（实时推事件流） |
| 7 | `webui/index.html` | ★ 可视化页面：会话列表 + 对话 + **日志轨迹实时生长** |
| 8 | `demo_provider.py` | 离线规则 provider（读历史、按关键词路由，让页面无 key 也能交互） |
| — | `demo.py` | 入口：离线跑完整故事（多轮 → 工具 → 退出 → resume → 同构 → 分叉） |
| — | `test_mini_harness.py` | 13 个装配验收测试（全离线） |
| — | `test_webui.py` | 11 个可视化服务测试（真起 HTTP 服务，全离线） |
| — | `llm_seam/ agent_loop/ tool_pipeline/ session/ runner.py` | 前四个练习的自包含副本（见下） |

> **自包含副本**：`llm_seam`(_01)、`agent_loop`(_04 版，含 resume 扩展)、
> `tool_pipeline`(_03)、`session`+`runner`(_04) 都是**原样拷贝**，本练习未改动它们
> 任何一行——这正是接缝价值的体现：组装不需要修改被组装者。

## 运行

```bash
cd P1_Coding/_05_Mini_Harness

# 可视化页面（推荐）：离线规则 provider，自动打开 http://127.0.0.1:8765/
./run_web.bat
bash run_web.sh
# 去掉 --fake 即走真实 API；可加 --port 9000 --search --deny-writes
python server.py --fake --no-open

# CLI 交互对话（真实 API，需要项目根 .env 里的 key）
./run.bat chat
./run.bat chat --session s1              # 同名会话即"恢复继续"
./run.bat chat --no-approve              # 写文件自动放行（跳过 y/n 审批）
./run.bat chat --search                  # 额外启用 web_search（需网络）

# 单条输入
./run.bat run "帮我算 987654*321，把结果写到 notes/result.txt" --no-approve

# 会话管理
./run.bat list
./run.bat show demo
./run.bat fork demo demo-fork --upto 6

# 离线完整演示（不需要 key）
./run.bat
bash run.sh

# 测试（全离线）
python -m pytest -q        # 13 + 11 = 24 个用例
```

## 可视化页面

`server.py` 用**标准库 http.server**（零新增依赖）把 harness 的事件流搬到浏览器：

- **三栏布局**：会话列表 | 对话 | **日志轨迹**（append-only 事件流）。
- **实时生长**：`POST /api/sessions/<id>/send` 以 NDJSON 流把每个事件边跑边推给前端，
  轨迹区随对话实时长出 `turn/step/assistant/tool` 各节点，跑完再从落盘日志重新加载权威视图
  （seq/时间戳以日志为准）。
- **工具栏**：`▶ 回放轨迹`（逐条动画播放）、`✂ 模拟崩溃`（往日志尾部写半行 JSON，再打开时
  自动修复并弹提示——M3 的核心机制一眼可见）、`⑂ 分叉`、`⟳ 刷新`。
- **离线可用**：`--fake` 用 `demo_provider.DemoProvider`——它读历史、按关键词路由
  （算术/写文件/读文件/列文件），因此页面上能真的看到"模型读到工具结果后再作答"的完整轨迹，
  且不需要 API key。

**安全提示（重要）**：页面把写文件审批默认设为 `AutoApprove`——浏览器里无法做 y/n 交互。
服务只监听 `127.0.0.1`，**没有任何认证**，仅适合本机演示；不要绑定到公网地址。
想观察"拒绝"路径用 `--deny-writes` 启动。

## 验收标准（M1–M3 组装）

- [x] **装配**：`MiniHarness.open` 建会话、装默认工具、把管线接进循环；system 进日志
- [x] **完整闭环**：模型申请 calculate → 管线执行 → 结果回填 → 最终回答；事件逐条落盘
- [x] **审批 + 沙箱**：write_file 批准后落盘 / 拒绝后不落盘 / 越界（`../`）放行也不许
- [x] **审批剧本纪律**：`ScriptedApprover` 精确匹配"被问到的次数"
- [x] **resume**：同 session_id 重开 → 历史完整、system 只注入一次、turn 编号接续
- [x] **工作区跨会话存活**：resume 后文件工具能看到上一段会话写的文件
- [x] **同构断言**：重放日志得到的历史 == 在线产生的历史 ★
- [x] **崩溃尾部修复**：半行 JSON 被自动截断，`open` 报告 `repaired`
- [x] **可选搜索**：默认不注册 web_search，`--search` 才启用
- [x] **CLI**：run / list / show / fork 全部可用；fork 目标已存在则 fail loud
- [x] **可视化服务**：GET 只读接口（config/tools/sessions/session）、POST send 以 NDJSON
      流回事件并落盘、fork、模拟崩溃 + 自动修复，11 个测试真起 HTTP 服务验证
- [x] `pytest -q` 全部通过（24 个用例，13 装配 + 11 可视化，全离线）
- [x] **真实 API 实测**：DeepSeek 完成 `calculate → write_file → 回答` 三步链；
      跨进程 resume 后 turn 接续、成功读回上一轮写的文件
- [x] **页面实测**：浏览器里多轮对话（算数/写文件/列文件）轨迹实时生长；崩溃按钮弹修复提示

## demo 实际输出（节选）

```
1) 新会话，多轮对话
   你：帮我算 1234*56.78        助手：1234 × 56.78 = 70066.52。
   你：把要点记到 notes/todo.txt  助手：已把笔记写入 notes/todo.txt。
   你：记住了吗？               助手：好的，我记住了。
2) 会话日志：session/start … turn/step … assistant/message … tool/result（27 条）
3) 退出：丢弃进程内对象，历史只留在磁盘日志里
4) resume：历史完整（11 条消息角色序列一致），turn 编号从 3 接续到 5
5) 同构断言：✓ 重放日志 == 在线历史
6) 分叉：demo → demo-fork（前 6 条事件），原会话 39 条未动
```

## 三个值得记住的设计点

1. **组装 = 接线，不是新增机制**。本练习的价值在于证明"四个接缝合起来就是一个产品"：
   唯一的"新代码"是 `MiniHarness`（把既有对象接起来）和工具面/CLI。
2. **resume 不是特例，是常态路径**。`open_session` 对新建与已存在是同一段代码；
   `MiniHarness.open` 亦然。历史永远来自 `derive_messages()`，"恢复"只是"日志非空"。
3. **工具面收敛成一处**。`build_workspace_registry` 是 harness 唯一声明"有哪些工具"的
   地方；加一个工具 = 加一条注册，循环/管线/日志都不动。

## 已知限制（承接前四个练习的债）

- 单会话单写者，无并发写锁；
- 工具面较小（无 shell / 无 subprocess，M5 的能力接缝）；
- 提示词未按 M4 做 section 装配（system 提示仍是单段字符串）；
- provider 仍是同步非流式 `complete()`；
- 多个练习各带一份同名包副本，全量测试靠 `conftest.py` 驱逐模块（`_05` 收敛成单一
  包后本可消除，但为保持"每个练习自包含"的约定仍保留了副本——取舍见决策记录 0005）。
