# Harness-Learning

从零实现一个 agent harness。参考 **DeepSeek Harness**（TypeScript）的机制，
用 **Python 3.13** 重写一遍。

每个阶段先定可运行的验收，验不过不进入下一阶段。

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 单文件预热：模型调用、工具调用、ReAct 循环、文件沙箱 | ✅ 4 个练习 / 15 用例 |
| **P1** | 最小骨架：Provider 协议、主循环、工具管线、会话日志、综合组装 | ✅ 5 个练习 / 79 用例 |
| **P2** | 能力补全：提示词装配、能力接缝、沙箱审批、插件协议、配置分层、RPC | ✅ 11 个阶段 / 693 用例 |
| **P3** | 插件化：内核（服务 / 事件 / effect / inject）+ 六个能力增量 | ✅ 6 个阶段 / 1576 用例 |
| **P4** | async-first 插件内核：服务 / effect / 事件 / 生命周期 / 启动器 | 🚧 `core/` + 测试 |

决策记录 58 份，见 [`docs/decisions/`](docs/decisions/)。

> **P4 当前状态**：async-first 内核（`core/` 四文件：context / plugin / launcher）
> 与机制测试（`tests/`）已完成；插件层与演示入口已从工作区移除。
> 恢复完整形态可用 `git checkout <commit> -- P4_Coding`。

---

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.13 |
| 依赖 | `openai`、`pydantic`、`PyYAML`、`pytest`、`pytest-asyncio`；可选 `ruff`、`mypy` |
| API 凭证 | 项目根 `.env` 中的 `DEEPSEEK_API_KEY`（已 gitignore） |

环境用项目根的 `.venv`（已 gitignore）：

```bash
python3.13 -m venv .venv
# Windows 用 .venv\Scripts\python，macOS / Linux 用 .venv/bin/python
.venv/bin/python -m pip install openai pydantic PyYAML pytest pytest-asyncio ruff
# 可选：静态类型检查（装了之后 P2/P3 门禁会顺带跑 mypy）
.venv/bin/python -m pip install mypy
```

跑测试 / 门禁前先激活 venv（`scripts/check.py` 从 PATH 找 ruff）：

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

`.env` 格式（只有真实 API 和讲稿页需要，离线测试不需要）：

```
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

> 值后面不要写行内注释——加载器不解析注释，会把它一起带进值里。

---

## 快速开始

### 跑测试

```bash
# 单个练习
cd P1_Coding/_05_Mini_Harness && python -m pytest -q

# P4 内核（离线，含并发与随机不变量测试）
cd P4_Coding && python -m pytest -q

# P2 / P3 各有独立门禁（ruff + 逐阶段 pytest）
cd P2_Coding && python scripts/check.py
cd P3_Coding && python scripts/check.py
```

### 跑演示

每个练习/阶段目录都自带 `run.bat`（Windows 双击）与 `run.sh`（Git Bash），
也可以直接 `python demo.py`。启动器优先用根目录 `.venv` 里的解释器，找不到就回退到 PATH 上的 `python`。

### 打开讲稿页

```bash
python docs/report/server.py          # http://127.0.0.1:8765/
python docs/report/server.py --open   # 顺手打开浏览器
```

讲稿页是一套 16:9 的演示文档，含交互 demo 和本机 dsh web 的内嵌弹窗。
页面自身的说明见 [`docs/report/README.md`](docs/report/README.md)。

> 直接双击 HTML 也能看，但聊天演示不可用——浏览器不能携带密钥请求模型，必须走 `server.py`。

---

## 目录结构

```
Harness-Learning/
├─ P0_Coding/           单文件预热（4 个练习）
├─ P1_Coding/           最小骨架（5 个练习）
├─ P2_Coding/           11 个最小扩展阶段 + mini_harness 整合包
├─ P3_Coding/           插件化内核 + 6 个能力增量
├─ P4_Coding/           插件内核 + 机制测试
│  ├─ core/             框架（context / plugin）+ 引导（launcher）
│  └─ tests/            机制测试（服务 / effect / 事件 / 生命周期 / 引导 / 并发）
├─ docs/
│  ├─ decisions/        57 份决策记录（背景 / 决策 / 备选 / 后果）
│  ├─ learning-plan.md  学习计划（M0–M8）
│  └─ report/           讲稿页与本地服务
└─ README.md
```

---

## 三条工程约束

1. **无验收不开始** —— 动手前先写下这一阶段的验收命令，它就是「完成」的定义。
2. **无笔记不算完成** —— 每个阶段留一份决策记录：背景、决策、备选、后果。
3. **门禁不绿不前进** —— ruff + mypy + pytest 全绿才进入下一阶段。

---

## 参考

- 参考实现：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（MIT）
- 设计论文：*A Programming Paradigm for Spatiotemporal Composability*（Cordis）

## 许可

本项目为个人学习项目。参考实现 dsh 以 MIT 许可发布，本项目不包含其源码。
