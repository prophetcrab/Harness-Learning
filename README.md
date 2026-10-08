# Harness-Learning —— agent harness 开发学习项目

从零学习 **agent harness**（智能体框架）开发的项目。参考实现是 TypeScript 的
**DeepSeek Harness**；本项目用 **Python 3.13** 重写它的各个机制，**只读参考、不抄代码**。

学习计划（M0–M8 阶段、铁律、读 dsh 清单）见 [docs/learning-plan.md](docs/learning-plan.md)。
本文件说明：**项目怎么组织、每阶段怎么跑、出问题怎么办**。

---

## 1. 阶段总览与路线图

项目按"预热 → 骨架 → 产品化"三级推进，每个阶段都是**自包含的模块化练习目录**
（`_NN_主题/`，四件套 = 入口 + README + 测试 + 一键启动器）：

| 阶段 | 内容 | 对应计划 | 状态 |
|---|---|---|---|
| **P0_Coding** | 单文件预热：把核心机制逐个跑通（模型调用、工具调用、循环、文件沙箱） | M0 前铺垫 | ✅ 完成（4 练习，15 用例） |
| **P1_Coding** | 结构化骨架：协议化 / 管线化 / 事件化 | M1–M3 | ✅ 完成（5 练习，79 用例） |
| **P2_Coding** | 产品化：提示词装配 / 能力接缝 / 配置组装 / 服务化 | M4–M7 | 🔵 进行中（M4 完成：`_01`–`_03`；M5 进行中：`_04`✅） |

每个阶段的入口 README：

- [P0_Coding/README.md](P0_Coding/README.md) —— 单文件练习索引
- [P1_Coding/README.md](P1_Coding/README.md) —— M1–M3 练习路线（简介 / 模块 / 用法）
- [P2_Coding/README.md](P2_Coding/README.md) —— M4–M7 的 11 个最小扩展阶段

**核心纪律**：无验收不开始、无笔记（决策记录）不算完成、门禁不绿不前进。

### 未来路线图（P3–P7，规划中）

P0→P1→P2 走完"预热 → 骨架 → 产品化"后，继续往下推进。**P3 起的顺序与定位如下**
（尚未开工，属规划；每阶段开工前再细化验收）：

| 阶段 | 目标 | 性质 | 对应计划 |
|---|---|---|---|
| **P3_Coding** | 补齐常规 harness 能力（流式、预算计量、压缩、spill、guard、任务管理 goal/plan/todo、上下文、凭证、沙箱…）；**范围由垂类裁剪，不求全** | 加功能 | M8 主线化 |
| **P4_Coding** | 不依赖 cordis 的 **Python 通用插件内核**（ctx 服务 + inject + 事件 + effect + 加载/卸载），把 harness 基本能力**重表达为插件**——最小自扩展底座 | **重构**（换骨架，功能不变） | — |
| **P5_Coding** | 选一个**垂类**，基于 P4 内核组合该领域少数深插件，补齐平台档（打包/文档/UI/凭证/安全），做出**可发布产品 demo** | 产品化 | — |
| **P6_Coding** | **前后端分离的 SDK 包 demo**：把内核/产品包成可嵌入的 SDK + 前端接入 | 分发形态 | — |
| **P7_Coding** | **回到 dsh 写插件**（建议按"练手"定义验收，贡献是副产品） | 回归参考实现 | M8 延伸 |

**四条排期要点**（这几轮讨论的结论，避免重复踩坑）：

1. **P7 建议提到 P4 之前**——P4 要设计内核 API，最好的老师是**先用成熟内核（cordis）写个真插件**；且 P7 不被任何决策阻塞，可立即开工。
2. **垂类必须在 P3 之前定死**——P3 的范围、需要哪些能力，全由垂类决定；不定，P3 会变成"我全都要"然后烂尾。
3. **P4 的标签是"重构"**——P3 在现有架构里做能力，P4 把它们改写成插件。会有重复劳动，但"功能不变、原测试全绿"是极硬的验收；先让它跑，再让它干净。
4. **P5/P6 的工作量里 harness 可能只占三成**——平台档（打包/文档/UI/遥测/凭证/安全）会从"不必追"变成"承重墙"，这是目标类型从"学机制"切到"做产品"的必然结果。

---

## 2. 环境准备

| 项目 | 说明 |
|---|---|
| Python | 3.13 |
| 依赖 | `openai`（DeepSeek 兼容 OpenAI 协议）、`pydantic`、`pytest`；工具链 `ruff`（可选 `mypy`） |
| API 凭证 | 项目根 `.env` 里的 `DEEPSEEK_API_KEY`（已被 `.gitignore` 排除，不入库） |

```bash
# 装依赖（任选：全局，或自建 .venv）
python -m pip install openai pydantic pytest ruff
```

**`.env` 格式**（项目根，真实 API 与可视化页面读它）：

```
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

> `DEEPSEEK_MODEL` 与 `DEEPSEEK_BASE_URL` 可省略（省略时用代码默认值）。
> **值后面不要写行内注释**——加载器不解析注释，会把注释文字一起带进值里。

> 所有 `run.bat` / `run.sh` / `run_web.*` 启动器优先用项目根的 `.venv/Scripts/python.exe`，
> **找不到就回退到 PATH 上的 `python`**。所以有没有 `.venv` 都能跑。

---

## 3. 怎么跑

### 方式 0：一键启动器（最省事）

每个练习/阶段目录都配了 `run.bat`（Windows，双击即用）与 `run.sh`（Git Bash）。

```bash
cd P1_Coding/_05_Mini_Harness
./run.bat                 # 双击等价：跑默认（离线 demo 或真实 API，见各阶段 README）
./run.bat --fake          # 传参透传给入口脚本
```

P2 各阶段另有 `run_web.bat` / `run_web.sh`（可视化页面）。

### 方式 A：直接用 python（推荐，命令直观）

```bash
cd P1_Coding/_05_Mini_Harness
python demo.py                          # 离线端到端 demo
python -m harness list                  # P2 阶段的统一入口
```

PowerShell 与 Git Bash 路径写法：Git Bash 用正斜杠 `/`，PowerShell 可用反斜杠 `\`。

> **Windows 提示**：若 `.bat` 里出现中文导致 cmd 解析错乱，记住约定——**`.bat` 保持纯 ASCII**，
> 中文输出全部交给 Python 脚本。中文乱码时用 Git Bash，或 `chcp 65001` 后重试。

---

## 4. 各阶段怎么用

每阶段的**招牌命令**；细节见各自目录的 README。

### P0_Coding（单文件预热，已完成）

```bash
cd P0_Coding/_03_Calculator_Loop
python calculator_loop.py --calc-only "2+3*4"   # 离线测计算器，不需要 key
python calculator_loop.py "帮我算 987654*321"    # 完整循环（需要 key）
```

### P1_Coding（M1–M3，已完成）

```bash
# _01 LLM 接缝      _02 主循环       _03 工具管线    _04 会话日志   _05 综合组装
cd P1_Coding/_05_Mini_Harness
python -m pytest -q                              # 22 用例（全离线）
python demo.py                                   # 离线：对话→工具→退出→resume→分叉
python app.py chat --session s1                  # 交互对话（可恢复）
python server.py                                 # 可视化页面（真实 API，:8765）
```

### P2_Coding（M4–M7，11 个最小扩展阶段）

```bash
cd P2_Coding/_01_Prompt_Sections                 # M4-1 提示词 section 注册表 + 装配器
python -m pytest -q                              # 41 用例（22 基线 + 19 本阶段）
python -m harness list                           # 统一入口
python chat.py --fake                            # 用装配出的提示词离线对话

cd P2_Coding/_02_Prompt_Context                  # M4-2 变量插值 + 运行时上下文（每 step 渲染）
python -m pytest -q                              # 57 用例（22 基线 + 35 本阶段）
python demo.py                                   # 离线演示：插值 / 每步渲染 / system 消息遮蔽
python chat.py --fake --ask "现在几点？"          # 每 step 渲染 {{time}}（真实 API 去掉 --fake）

cd P2_Coding/_03_Prompt_Trace                    # M4-3 装配单 + 来源追溯 + 重建断言（M4 收官）
python -m pytest -q                              # 49 用例（22 基线 + 27 本阶段）
python chat.py --fake --ask "现在几点？" --dump-prompt   # 装配单：来源/变量/重建校验
python demo.py                                   # 离线演示：装配单 / 篡改检测 / 逐步重建

cd P2_Coding/_04_Filesystem_Seam                 # M5-1 FileSystem 接缝（Local/Memory 可换）
python -m pytest -q                              # 76 用例（22 基线 + 54 本阶段，双 provider 套件）
python chat.py --fake --fs memory --ask "把 hello 写到 notes/a.txt"  # 磁盘零痕迹
python demo.py                                   # 离线演示：接缝对照 / 单槽服务 / 双 provider 同结果

cd P2_Coding/_05_Workspace_Jail                  # M5-2 工作区围栏（策略型 provider）
python -m pytest -q                              # 75 用例（22 基线 + 53 本阶段）
python chat.py --fake --fs jail --ask "把 x 写到 ../escape.txt"   # 越界 → 结构化拒绝
python demo.py                                   # 离线演示：围栏 / 审批放行也拦 / 三 provider 对照
```

各阶段对应机制见 [P2_Coding/README.md](P2_Coding/README.md) 的 11 行总览表
（`_01`–`_03` 提示词装配 · `_04`–`_06` 能力接缝 · `_07`–`_09` 组合与配置 · `_10`–`_11` 服务化）。

---

## 5. 跑测试与门禁

```bash
# 一次跑完所有阶段（从项目根目录）
python -m pytest P0_Coding P1_Coding P2_Coding -q   # 当前共 524 个用例（P0 15 + P1 79 + P2 430）

# 只跑某一个练习/阶段
cd P1_Coding/_03_Tool_Pipeline && python -m pytest -q

# P2 有独立门禁（ruff + 逐阶段 pytest）
cd P2_Coding && python scripts/check.py
```

测试默认**不联网、不消耗模型额度**：模型调用一律走 `FakeLLM` 离线剧本断言，
真实 API 只用于 demo 与可视化页面。

---

## 6. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'openai'` | 用的 Python 没装依赖 | `python -m pip install openai pydantic pytest`，或让启动器找到 `.venv` |
| `未找到 DEEPSEEK_API_KEY` | 根目录 `.env` 缺失或格式不对 | 按 §2 建 `.env`；离线体验用 `--fake` / `--calc-only` |
| `SSLError` / `ConnectionError` / 请求超时 | 网络问题（模型接口或搜索不可达） | 检查网络/代理；本站 `github.com:443` 偶发不可达，git push 可走本机代理 |
| 中文输出乱码 | 终端编码问题 | 用 Git Bash，或 `chcp 65001` 后重试 |
| PowerShell 报"禁止运行脚本" | 执行策略限制 `Activate.ps1` | 别激活环境，直接用 `python` 或全路径 `python.exe` |
| 搜索结果全是无关内容 | Bing 页面结构变化导致解析失效 | 已知限制，先用浏览器确认 `cn.bing.com` 正常 |

---

## 7. 约定

- **目录命名**：`P<N>_Coding/_NN_主题/`，每个阶段自包含（各带所需的包副本）。
- **代码组织（P2）**：`harness/` 冻结为 M1–M3 基线库；各阶段新增机制放阶段主目录顶层
  （与 `harness/` 平级，如 `_01_Prompt_Sections/prompt/`），让「这一阶段加了什么」目录层面可见。
- **四件套**：入口脚本 + `README.md`（目的/验收/运行）+ `test_*.py` + `run.bat`/`run.sh`。
- **先写验收标准，再写实现**；验收不过不算完成。
- **测试离线可重复**：模型调用走 `FakeLLM`；需要网络/真实 API 的路径用旁路 demo 隔离。
- **`.bat` 保持纯 ASCII**（注释与提示用英文）——`chcp 65001` 后出现中文会让 cmd 解析错位。
- P1 起每个练习/阶段完成时，在 [docs/decisions/](docs/decisions/README.md) 写决策记录
  （背景 / 决策 / 备选 / 后果）；编号全项目递增，不复用。

---

## 8. 目录结构

```
Harness-Learning/
├── README.md                     ← 本说明书
├── .env                          ← API 凭证（不入库）
├── .gitignore
├── docs/
│   ├── learning-plan.md          ← 学习计划（M0–M8 阶段与进度表）
│   └── decisions/                ← 决策记录（0001 起，格式见其 README）
├── P0_Coding/                    ← 阶段 0：单文件预热（已完成）
│   ├── _01_LLM_Calling/          ← 模型调用 + 提示词拼装
│   ├── _02_Tool_Calling/         ← 工具调用 + 网络搜索
│   ├── _03_Calculator_Loop/      ← 模型→工具→模型 循环
│   └── _04_File_Tools_Loop/      ← 文件读写工具（沙箱 + 审批）
├── P1_Coding/                    ← 阶段 1：结构化骨架（M1–M3，已完成）
│   ├── _01_Provider_Protocol/    ← LLM 接缝（协议 + FakeLLM + DeepSeek）
│   ├── _02_Agent_Loop/           ← 主循环（turn/step + 轨迹 + 取消）
│   ├── _03_Tool_Pipeline/        ← 工具管线（注册表 + 审批 + pre/exec/post）
│   ├── _04_Session_Log/          ← 会话日志（事件溯源 + JSONL + resume）
│   └── _05_Mini_Harness/         ← 综合组装 + 可视化页面
└── P2_Coding/                    ← 阶段 2：产品化（M4–M7，进度见其 README）
    ├── README.md                 ← 11 阶段总览 + 模块简介 + 使用方法
    ├── pyproject.toml            ← ruff 配置（阶段式工作区）
    ├── scripts/check.py          ← 门禁：ruff + 逐阶段 pytest
    ├── _01_Prompt_Sections/      ← M4  section 注册表 + 装配器
    ├── _02_Prompt_Context/       ← M4  变量插值 + 运行时上下文
    ├── _03_Prompt_Trace/         ← M4  --dump-prompt + 重建断言
    ├── _04_Filesystem_Seam/      ← M5  FileSystem 接缝 + Local/Memory
    ├── _05_Workspace_Jail/       ← M5  WorkspaceJailFS 策略
    ├── _06_Subprocess_Seam/      ← M5  Subprocess 接缝 + shell
    ├── _07_Plugin_Effect/        ← M6  插件协议 + effect 回卷
    ├── _08_Profile_Layers/       ← M6  YAML 分层 patch
    ├── _09_Dump_Config/          ← M6  dump-config + 错误定位
    ├── _10_Rpc_Transport/        ← M7  stdio JSON-RPC
    └── _11_Session_Follow/       ← M7  follow + attach
```

每个练习/阶段目录的内部结构（入口 + 启动器 + 测试 + README）见各自 README。
