# AgentDevLearn 使用说明书

从零学习 agent harness 开发的项目。本文件说明：**环境怎么配、每个脚本怎么跑、出问题怎么办**。
学习计划本身见 [docs/learning-plan.md](docs/learning-plan.md)。

---

## 1. 快速开始（30 秒版）

打开终端，复制粘贴：

```bash
cd D:/project/AgentDevLearn/P0_Coding/_01_LLM_Calling
D:/project/AgentDevLearn/.venv/Scripts/python.exe llm_calling.py "什么是 agent？"
```

就把问题发给了 DeepSeek。下面是完整说明。

---

## 2. 环境准备（已完成，仅供重建时参考）

| 项目 | 位置 | 说明 |
|---|---|---|
| Python 环境 | `D:\project\AgentDevLearn\.venv` | Python 3.13 虚拟环境，已装 openai / pytest 等 |
| API 凭证 | `D:\project\AgentDevLearn\.env` | 存放 `DEEPSEEK_API_KEY`（已被 `.gitignore` 排除，不会入库） |

**重建环境**（换了电脑或环境损坏时）：

```bash
cd D:/project/AgentDevLearn
python -m venv .venv
.venv/Scripts/python.exe -m pip install openai pytest pydantic
```

**`.env` 文件内容格式**：

```
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_MODEL=deepseek-chat                  # 可选
DEEPSEEK_BASE_URL=https://api.deepseek.com    # 可选
```

---

## 3. 运行脚本的方式（任选一种）

所有脚本都遵循同一模式：**用 `.venv` 里的 Python 直接运行 `.py` 文件，不用手动激活环境**。

### 方式 0：一键运行（最省事，不用记 Python 路径）

每个练习目录里都配了启动器：`run.bat`（Windows / 双击即用）和 `run.sh`（Git Bash）。

```bash
cd D:/project/AgentDevLearn/P0_Coding/_01_LLM_Calling

./run.bat "你的问题"        # Git Bash 里直接跑（cmd 里直接用 run.bat "你的问题"）
bash run.sh "你的问题"      # Git Bash 备选
```

**双击 `run.bat`**：会打开窗口问你问题（直接回车就用默认问题），跑完暂停、窗口不关。

- PowerShell 里要加 `.\`：`.\run.bat "你的问题"`
- 不带参数直接双击或运行：使用默认问题
- 启动器会用 `.venv` 里的 Python，找不到环境会明确报错

### 方式 A：进入脚本目录，用相对路径运行（推荐，命令短）

```bash
cd D:/project/AgentDevLearn/P0_Coding/_01_LLM_Calling
../../.venv/Scripts/python.exe llm_calling.py "你的问题"
```

**方式 B：在项目根目录，用全路径运行**

```bash
cd D:/project/AgentDevLearn
.venv/Scripts/python.exe P0_Coding/_01_LLM_Calling/llm_calling.py "你的问题"
```

**方式 C：先激活虚拟环境，之后直接用 `python`**

```bash
cd D:/project/AgentDevLearn
source .venv/Scripts/activate        # Git Bash
# 或 PowerShell: .\.venv\Scripts\Activate.ps1
python P0_Coding/_01_LLM_Calling/llm_calling.py "你的问题"
deactivate                            # 退出环境
```

> **Windows 提示**
> - PowerShell 里路径可用反斜杠：`D:\project\AgentDevLearn\.venv\Scripts\python.exe`；Git Bash 里用正斜杠 `/`。
> - 如果不激活环境，PowerShell 执行策略不会拦任何东西，这是最省事的路子。
> - 中文乱码时，先确认用的是 `.venv` 的 Python（脚本内已强制 UTF-8 输出）。

---

## 4. 脚本清单

### _01_LLM_Calling —— 模型调用 + 提示词拼装

**干什么**：把三个提示词小节（角色设定 / 工作环境 / 输出要求）装配成一个 system 提示词，
发给 DeepSeek 模型并流式打印回答。演示"提示词是装配出来的"。

**怎么跑**：

```bash
cd D:/project/AgentDevLearn/P0_Coding/_01_LLM_Calling

# 一键运行（双击 run.bat 等价）
./run.bat                        # 不带参数：问默认问题
./run.bat "用一句话解释什么是事件溯源"
bash run.sh "用一句话解释什么是事件溯源"

# 手动运行
../../.venv/Scripts/python.exe llm_calling.py
../../.venv/Scripts/python.exe llm_calling.py "用一句话解释什么是事件溯源"
```

**运行后会看到**：装配好的 system 提示词 → 本次问题 → 模型回答（流式逐字打印）。

**改提示词**：编辑 `llm_calling.py` 开头第 1 节的 `sections` 列表即可。

**测试**：

```bash
../../.venv/Scripts/python.exe -m pytest -q     # 2 个用例，不联网
```

---

### _02_Tool_Calling —— 工具调用 + 结果展示（网络搜索）

**干什么**：给模型一个 `web_search` 工具（Bing 搜索，无需搜索服务 API key）。
模型会自主决定是否调用它，脚本执行搜索、展示结果、把结果回填给模型，模型再作答。
配套的 `inspect_model_calls.py` 把每一次模型调用的请求与原始返回完整摊开（观察模型无状态、tool_calls 结构）。

**怎么跑**：

```bash
cd D:/project/AgentDevLearn/P0_Coding/_02_Tool_Calling

# 一键运行（双击 run.bat 等价）
./run.bat "今天上海天气怎么样？"                # 完整链路：模型自主搜索
./run.bat --search-only "python 教程"          # 只测搜索工具，不调模型
./run.bat inspect "今天上海天气怎么样？"        # 观察每次模型调用的请求与返回
bash run.sh "今天上海天气怎么样？"

# 手动运行
../../.venv/Scripts/python.exe tool_calling.py "今天上海天气怎么样？"
../../.venv/Scripts/python.exe tool_calling.py --search-only "python 教程"
../../.venv/Scripts/python.exe inspect_model_calls.py "今天上海天气怎么样？"
```

**运行后会看到**：

```
--- 第 1 轮：请求模型 ---
[工具调用] web_search({"query": "上海今天天气"})   ← 模型请求调用
[工具结果] 关键词「上海今天天气」共 5 条：...       ← 脚本真正执行
--- 第 2 轮：请求模型 ---
=== 最终回答 ===                                  ← 模型基于结果作答
```

**测试**：

```bash
../../.venv/Scripts/python.exe -m pytest -q     # 2 个用例（联网用例断网自动跳过）
```

---

### _03_Calculator_Loop —— 模型 → 工具 → 模型的循环演示（计算器）

**干什么**：换一个**完全离线**的计算器工具，把"模型 ⇄ 工具"的循环本身演示清楚。
运行日志逐轮打印"申请了哪些工具、执行了什么、结果是什么、发出了多少条消息"，并附运行统计。
内置 `--no-tools` 对照实验和安全要点（AST 白名单求值，绝不 `eval` 模型生成的字符串）。

**怎么跑**：

```bash
cd D:/project/AgentDevLearn/P0_Coding/_03_Calculator_Loop

# 一键运行（双击 run.bat 等价）
./run.bat                                       # 循环演示（默认问题）
./run.bat "帮我算 987654 * 321"                  # 指定问题
./run.bat --no-tools "帮我算 987654 * 321"       # 对照实验：不给工具，模型只能心算
./run.bat --calc-only "2+3*4"                   # 只测计算器，不需要 key

# 手动运行
../../.venv/Scripts/python.exe calculator_loop.py
../../.venv/Scripts/python.exe calculator_loop.py --calc-only "(100+20)/3"
```

**测试**：

```bash
../../.venv/Scripts/python.exe -m pytest -q     # 5 个用例（全部离线，不需要 key）
```

---

### _04_File_Tools_Loop —— 带文件读写工具的循环（含沙箱与审批）

**干什么**：在循环里加入 `read_file` / `write_file` / `list_files`，凑齐最小 harness 的核心工具面
（文件 + 计算 + 网络）。这是第一次给模型**写权限**，因此带两个安全机制：
工作区沙箱（路径越界直接拒绝）和写操作人工审批（终端 y/n，无终端时 fail-closed 拒绝）。

**怎么跑**：

```bash
cd D:/project/AgentDevLearn/P0_Coding/_04_File_Tools_Loop

# 一键运行（双击 run.bat 等价；写文件时会问你 y/n）
./run.bat                                        # 默认问题：改写工作区的 项目介绍.md
./run.bat "在工作区新建一个 README.md，写三行介绍"
./run.bat --no-approve "在工作区新建一个 demo.txt"  # 自动放行写入（仅演示用）
./run.bat --tool-only list_files                  # 只测文件工具，不需要 key

# 手动运行
../../.venv/Scripts/python.exe file_tools_loop.py
../../.venv/Scripts/python.exe file_tools_loop.py --tool-only "项目介绍.md"
```

**测试**：

```bash
../../.venv/Scripts/python.exe -m pytest -q     # 5 个用例（全部离线，不需要 key）
```

---

## 5. 跑测试（两种粒度）

```bash
# 一次跑完所有练习的测试（从项目根目录）
cd D:/project/AgentDevLearn
.venv/Scripts/python.exe -m pytest P0_Coding P1_Coding -q   # 当前共 96 个用例（P0 15 + P1 81）

# 只跑某一个练习
cd P1_Coding/_03_Tool_Pipeline
../../.venv/Scripts/python.exe -m pytest -q
```

测试默认**不联网、不消耗模型额度**（`_02` 的联网用例断网时会自动跳过）。

---

## 6. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'openai'` | 用错了 Python（不是 `.venv` 里的） | 检查命令里的 python 路径是否以 `AgentDevLearn/.venv/Scripts/python.exe` 结尾 |
| `未找到 DEEPSEEK_API_KEY` | 根目录 `.env` 缺失或格式不对 | 按 §2 重建 `.env`；只想验证搜索时用 `--search-only` |
| `SSLError` / `ConnectionError` / 请求超时 | 网络问题（模型接口或 Bing 不可达） | 检查代理/网络；搜索单独排查用 `--search-only` |
| 中文输出乱码 | 终端编码问题 | 用 Git Bash，或 `chcp 65001` 后重试 |
| 搜索结果全是无关内容 | Bing 页面结构变化导致解析失效 | 属于已知限制，先用浏览器打开 `cn.bing.com` 确认网页正常 |
| PowerShell 报"禁止运行脚本" | 执行策略限制 `Activate.ps1` | 别激活环境，直接用 `.venv\Scripts\python.exe` 全路径运行 |

---

## 7. 新增练习的约定

- 目录命名：`P0_Coding/_NN_主题/`（P1 起为 `P1_Coding/_NN_主题/`）。
- 脚本风格：**单文件平铺**，按注释分节，从上到下直接执行；不写 `main()`、不用 argparse。
  （P1 起结构升级为可导入的模块，测试从黑盒升级为单元测试 + FakeLLM，见 [P1_Coding/README.md](P1_Coding/README.md)。）
- 每个练习包含四件套：主脚本 + `README.md`（目的/验收标准/运行方式）+ `test_*.py` 验收测试 + `run.bat`/`run.sh` 一键启动器。
- 先写验收标准，再写实现；验收不过不算完成。
- 测试尽量离线可重复：网络/模型调用用 `--search-only` 这类旁路模式隔离。
- **`.bat` 文件保持纯 ASCII**（注释和提示都用英文）——`chcp 65001` 之后出现中文会让 cmd 解析错位、执行到注释碎片；中文输出全部交给 Python 脚本负责。
- P1 起每个练习完成时，在 [docs/decisions/](docs/decisions/README.md) 写一份决策记录。

---

## 8. 目录结构

```
AgentDevLearn/
├── README.md                     ← 本说明书
├── .env                          ← API 凭证（不入库）
├── .gitignore
├── .venv/                        ← Python 3.13 虚拟环境
├── docs/
│   ├── learning-plan.md          ← 学习计划（M0-M8 阶段与进度表）
│   └── decisions/                ← 决策记录（P1 起，格式见其 README）
├── P0_Coding/                    ← 阶段 0：单文件预热练习（已完成）
│   ├── README.md                 ← 练习索引
│   ├── _01_LLM_Calling/          ← 模型调用 + 提示词拼装
│   ├── _02_Tool_Calling/         ← 工具调用 + 网络搜索（含 inspect_model_calls.py）
│   ├── _03_Calculator_Loop/      ← 模型→工具→模型 循环（计算器）
│   └── _04_File_Tools_Loop/      ← 文件读写工具（沙箱 + 审批）
└── P1_Coding/                    ← 阶段 1：结构化骨架（M1–M3 五个练习已完成）
    ├── README.md                 ← 路线图：_01 Provider → _05 Mini Harness
    ├── _01_Provider_Protocol/    ← 练习 1：LLM 接缝（协议 + FakeLLM + DeepSeek）
    │   ├── llm_seam/                包源码（6 个模块，按依赖顺序阅读）
    │   ├── demo.py                  入口（--fake 离线 / 默认真实 API）
    │   ├── run.bat / run.sh         一键运行
    │   ├── test_provider_protocol.py 验收测试（12 用例，全离线）
    │   └── README.md
    ├── _02_Agent_Loop/           ← 练习 2：Agent 主循环（turn/step + 轨迹 + 取消）
    │   ├── agent_loop/              包源码（trace 轨迹词汇 + loop 主循环）
    │   ├── llm_seam/                自包含副本（来自 _01，去掉 loop.py）
    │   ├── demo.py                  入口（--fake 离线 / 默认真实 API）
    │   ├── run.bat / run.sh         一键运行
    │   ├── test_agent_loop.py       验收测试（13 用例，全离线）
    │   └── README.md
    ├── _03_Tool_Pipeline/        ← 练习 3：工具管线（注册表 + 审批 + pre/exec/post）
    │   ├── tool_pipeline/           包源码（registry/schema/approval/pipeline/middlewares/builtin）
    │   ├── agent_loop/ llm_seam/    自包含副本（来自 _02/_01，均未修改）
    │   ├── demo.py                  入口（--fake / --pipeline-only / 默认真实 API）
    │   ├── run.bat / run.sh         一键运行
    │   ├── test_tool_pipeline.py    验收测试（19 用例，全离线）
    │   └── README.md
    ├── _04_Session_Log/          ← 练习 4：会话日志（事件溯源 + JSONL + resume）
        ├── session/                 包源码（events/projection/log/store/recorder）
        ├── runner.py                胶水：open_session / Runner / fork_session
        ├── cli.py                   会话 list / show / run / fork
        ├── agent_loop/ llm_seam/    自包含副本（agent_loop 有 resume 扩展）
        ├── demo.py                  入口：全套离线演示（含模拟崩溃恢复）
        ├── run.bat / run.sh         一键运行
        ├── test_session_log.py      验收测试（13 用例，全离线）
        └── README.md
    └── _05_Mini_Harness/         ← 练习 5：综合组装（可对话 + 可恢复 + 可视化）
        ├── mini_harness.py          装配核心：MiniHarness（provider + 管线 + 循环 + 日志）
        ├── workspace_tools.py       工具面：calculate / read_file / write_file / list_files
        ├── web_search.py            可选工具：Bing 搜索（--search 才注册）
        ├── app.py                   CLI：chat / run / list / show / fork
        ├── server.py                可视化服务（stdlib http.server + NDJSON 事件流）
        ├── webui/index.html         可视化页面：对话 + 日志轨迹实时生长
        ├── demo_provider.py         离线规则 provider（页面无 key 也能交互）
        ├── runner.py agent_loop/ llm_seam/ tool_pipeline/ session/
        │                            前四个练习的自包含副本（均未修改）
        ├── demo.py                  入口：离线跑完整故事（对话→工具→退出→resume→分叉）
        ├── run.bat / run.sh         一键运行（demo / CLI）
        ├── run_web.bat / run_web.sh 一键运行（可视化页面）
        ├── test_mini_harness.py     装配验收测试（13 用例，全离线）
        ├── test_webui.py            可视化服务测试（11 用例，真起 HTTP）
        └── README.md
```

每个练习目录的内部结构（入口脚本 + `run.bat`/`run.sh` + 测试 + README）见各练习自己的 README。
