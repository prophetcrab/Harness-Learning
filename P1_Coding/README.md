# P1_Coding

## P1 简介

P1 是"从跑通机制到装进结构"的阶段。P0 用单文件脚本把 harness 的核心机制逐个摸了一遍；
P1 把这些机制升级成一个小 harness 的骨架——**协议化、管线化、事件化**，最终得到
"能对话、能恢复、可扩展工具、可离线测试、可可视化观察"的 mini harness。

对应学习计划的 **M1 → M2 → M3**，共 5 个练习，依赖关系
`_01 → _02 → {_03, _04} → _05`：

| 编号 | 主题 | 对应阶段 |
|---|---|---|
| `_01_Provider_Protocol` | LLM 接缝（协议 + FakeLLM + DeepSeek） | M1 |
| `_02_Agent_Loop` | Agent 主循环（turn/step + 轨迹 + 取消） | M1 |
| `_03_Tool_Pipeline` | 工具管线（注册表 + 审批 + pre/execute/post） | M2 |
| `_04_Session_Log` | 会话日志（事件溯源 + JSONL + resume） | M3 |
| `_05_Mini_Harness` | 组装（可对话 + 可恢复的小 harness + 可视化页面） | M1–M3 验收 |

**状态：5 个练习全部完成，79 个 P1 用例全离线通过**（连同 P0 共 94 个）。
每个练习自包含（各带所需的同名包副本），并配一份决策记录 `docs/decisions/000N-*.md`。

> **后续阶段见 [P2_Coding](../P2_Coding/README.md)**：P2 已把这里的四份副本收敛成
> 单一 `harness/` 包并补上门禁，继续做 M4–M7（提示词装配 / 能力接缝 / profile / 服务化）。
> P1 的五个练习作为教学快照保留；后续以 P2 为唯一维护对象。

约定：测试一律走 `FakeLLM` 离线断言，真实 API 只用于 demo；`.bat` 保持纯 ASCII；
每个练习四件套 = 入口脚本 + README + 测试 + 一键启动器。

---

## 模块简介

### `_01_Provider_Protocol` —— LLM 接缝
把散落的模型调用升级成**三角色接缝**：定义（`LLMProvider` 协议 + 中立词汇
`Message`/`ToolCall`/`ToolSpec`）、实现（`FakeLLM` 离线剧本 / `DeepSeekProvider` 真实）、
消费者（`run_tool_loop`）。厂商方言（JSON 字符串参数、`tool_call_id` 配对）全部关在
provider 里。**换供应商只改一行组装代码。** 交付：`llm_seam/` 包 + 12 个离线测试。

### `_02_Agent_Loop` —— Agent 主循环
把闭环函数升级成可复用的 `AgentLoop` 类，引入 **turn/step 两层词汇**：一次 `run()` 是一个
turn，turn 内部每轮模型往返是一个 step；产出可事后审查的**调用轨迹**
（`TurnResult → Step → ToolResult`）；支持在 step 边界取消。交付：`agent_loop/` 包 + 13 个测试。

### `_03_Tool_Pipeline` —— 工具管线
把"注册 + 查表"升级成 dsh 式的**注册表 + 执行管线**：定义与实现分离（`define_tool`）、
两层作用域遮蔽、`pre → execute → post` 三段中间件链（决策/改写参数 → 执行+超时 → 加工结果）、
审批策略接口（allow/deny/ask，**问不到应答方一律 fail-closed**）。交付：`tool_pipeline/` 包
+ 19 个测试。

### `_04_Session_Log` —— 会话日志（全项目的心脏）
把内存轨迹升级成**事件溯源**：会话是一串 append-only 事件，模型历史由 `derive_messages()`
从日志**投影**而来（绝不直接存）；每条事件落盘 `session.jsonl`，进程被强杀留下的半截尾行
下次启动自动截断修复；`open_session` 对新建/已存在是同一条路径，**resume 不是特例而是常态**。
交付：`session/` 包 + `runner.py` + `cli.py` + 13 个测试。

### `_05_Mini_Harness` —— 综合组装 + 可视化
把前四者接成一个能用的产品——**被组装的四个包一行未改**：`MiniHarness` 装配类把 provider、
工具管线、主循环、会话日志接起来；默认工具面 `calculate / read_file / write_file / list_files`
（工作区沙箱 + 写审批），可选 `web_search`；提供 CLI（`app.py`）与**可视化页面**（`server.py`
+ `webui/index.html`）。交付：装配类 + CLI + 可视化服务 + 24 个测试。

---

## 使用方法

所有命令都在**对应练习目录下**运行。若项目根有 `.venv`，`run*.bat` / `run*.sh`
会自动使用它，否则回退到 PATH 上的 `python`。

### `_01_Provider_Protocol`

```bash
cd P1_Coding/_01_Provider_Protocol
python -m pytest -q                     # 12 用例（全离线）
python demo.py --fake                   # 离线剧本，看"换 provider 只改一行"
python demo.py "帮我算 1234*56.78"       # 真实 API（需 .env 里的 key）
./run.bat --fake                        # 一键（双击 run.bat 亦可）
```

### `_02_Agent_Loop`

```bash
cd P1_Coding/_02_Agent_Loop
python -m pytest -q                     # 13 用例（全离线）
python demo.py --fake                   # 看 turn/step 轨迹（1 turn 内含多个 step）
python demo.py "帮我算 987654*321 再除以 7"
./run.bat --fake
```

### `_03_Tool_Pipeline`

```bash
cd P1_Coding/_03_Tool_Pipeline
python -m pytest -q                     # 19 用例（全离线）
python demo.py --pipeline-only          # 只看管线：成功/超时/截断/审批三态/沙箱越界
python demo.py --fake                   # 管线 + 主循环闭环
./run.bat --pipeline-only
```

### `_04_Session_Log`

```bash
cd P1_Coding/_04_Session_Log
python -m pytest -q                     # 13 用例（全离线）
python demo.py                          # 离线全套：落盘 → 模拟崩溃 → 修复 → resume → 同构 → fork
python cli.py list                      # 会话列表
python cli.py show <id>                 # 看事件日志
python cli.py run <id> "问题" --fake     # 跑一个 turn（不存在即创建，存在即恢复）
python cli.py fork <src> <new> --upto 4  # 分叉
```

### `_05_Mini_Harness`

```bash
cd P1_Coding/_05_Mini_Harness
python -m pytest -q                     # 22 用例（全离线）

# —— 可视化页面（推荐，直观看到对话与日志轨迹）——
./run_web.bat                           # 真实 API（读取根目录 .env 里的 key），自动开浏览器
python server.py --port 8765 --no-open   # 手动指定端口/不自动开页面
# 页面默认 http://127.0.0.1:8765/
#   三栏：会话列表 | 对话 | 日志轨迹（append-only 事件流，实时生长）
#   工具栏：▶ 回放轨迹 / ✂ 模拟崩溃（看自动修复）/ ⑂ 分叉 / ⟳ 刷新
#   可选参数：--search（启用联网搜索）--deny-writes（禁止写操作，观察拒绝路径）

# —— CLI ——
python app.py chat --fake               # 交互对话（可 /history、/exit；同名会话即恢复）
python app.py chat                      # 真实 API 交互对话
python app.py chat --no-approve         # 写文件自动放行
python app.py run "帮我算 2+3" --fake     # 一条输入跑一个 turn
python app.py list                      # 会话列表
python app.py show default              # 看事件日志
python app.py fork default default-fork --upto 6

# —— 离线完整故事（脚本化，无需交互）——
python demo.py                          # 多轮对话 → 工具 → 退出 → resume → 同构 → 分叉
```

**常见的可视化 / 测试姿势**（页面里最值得点一遍的几条路径）：

1. **对话 + 工具**：输入"帮我算 987654\*321"——右侧轨迹实时长出
   `turn 开始 → step → 模型回复(tool_calls) → 工具结果 → 模型回复(最终回答)`。
2. **日志轨迹回放**：点 `▶ 回放轨迹` 逐条播放当前会话的事件；点会话列表切换不同会话对照。
3. **崩溃恢复**：点 `✂ 模拟崩溃`（往日志尾部写半行 JSON）→ 顶部弹出"已自动修复，丢弃 N 字节"，
   事件不丢——这是 M3 最核心的机制，可视化后一目了然。
4. **审批第二道防线**：用 `--deny-writes` 启动，再让它写文件，可见 `tool/result` 变红（被拒绝）；
   沙箱越界（`../x`）即使放行也会被拦。
5. **分叉**：点 `⑂ 分叉` 从当前会话某条事件派生新会话，原会话不变。

> 从零跑通全部：`python -m pytest P0_Coding P1_Coding -q` → 94 passed。

---

## 运行约定

与 P0 相同：用 `.venv` 里的 Python 直接运行各练习目录的入口脚本；每个练习配
`run.bat` / `run.sh`（`_05` 另有 `run_web.bat` / `run_web.sh`）。
`_02`–`_05` 各自带同名顶层包，全量 `pytest` 时由各练习的 `conftest.py` 驱逐同名模块以避免串味
（`_05` 收敛成单一包后可消除，取舍见决策记录 0005）。环境配置与常见问题见[项目根 README](../README.md)。
