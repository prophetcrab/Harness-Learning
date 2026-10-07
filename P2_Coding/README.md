# P2_Coding

## P2 简介

P2 是"把 harness 长成一个真正的产品"的阶段。P1 结束时你有一个能对话、能恢复、可扩展工具、
可离线测试的小 harness，但它是**四份自包含副本拼起来的**、组装发生在练习目录里。
P2 在单一 `harness/` 包上做四件事：**提示词变成可装配的产物 → 能力变成可替换的接缝 →
组合变成配置数据 → harness 变成常驻服务**。

对应学习计划的 **M4 → M5 → M6 → M7**，四个阶段各自一个自包含目录：

| 编号 | 主题 | 对应阶段 | 核心交付 |
|---|---|---|---|
| `_01_Prompt_Assembly` | 提示词装配 | M4 | section 注册表 + 变量插值 + `--dump-prompt` |
| `_02_Capability_Seams` | 能力接缝 | M5 | `FileSystem`/`Subprocess` 三角色 + Memory/Jail provider |
| `_03_Profile_Composition` | Profile 组装 | M6 | 插件协议 + YAML 分层 patch + `dump-config` |
| `_04_Service` | 服务化 | M7 | JSON-RPC `serve` + `attach`（follow = 重放 + 订阅） |

依赖关系：`_01 → _02 → _03 → _04`（后一阶段从上一阶段的 `harness/` 复制起点）。

**沿用 P1 的约定**：每个阶段自包含、四件套（入口 + README + 测试 + 一键启动器）、
先定验收再实现、测试走 `FakeLLM` 离线断言、真实 API 只用于 demo。
**与 P1 的差异**：阶段之间**不共享代码**——每个 `_0N` 目录自带一份完整 `harness/` 副本。

## 目录

```
P2_Coding/
├── README.md            ← 本文件
├── pyproject.toml       ← ruff 配置（阶段式工作区，不做 setuptools 打包）
├── scripts/check.py     ← 门禁：ruff（整体）+ pytest（逐阶段）
├── _01_Prompt_Assembly/     ← M4：提示词装配
├── _02_Capability_Seams/    ← M5：能力接缝
├── _03_Profile_Composition/ ← M6：Profile 组装
└── _04_Service/             ← M7：服务化
```

每个 `_0N` 目录的内部结构一致：

```
_0N_<主题>/
├── README.md          ← 本阶段的学习计划（读 dsh / 做 / 验收 / 运行）
├── harness/           ← 一份完整的 harness 副本（llm/agent/tools/session 基线 + 各阶段子包）
│   ├── prompt/ providers/ config/ server/   ← M4–M7 的实现落点
│   └── ...（M1–M3 的基线代码）
├── tests/             ← 基线验收测试（22 用例，M1–M3 组装回归）
├── demo.py            ← 离线端到端回归
├── conftest.py        ← pytest 引导（隔离本阶段的 harness 副本）
└── run.bat / run.sh   ← 一键启动器
```

四个阶段现在都是**同一份基线代码**（P1 `_05` 语义等价的单一包）。M4–M7 的实现尚未落地——
对应子包（`prompt` / `providers` / `config` / `server`）目前是只有规划说明的占位包。

---

## 模块简介与使用方法

每个阶段都能单独跑。通用命令（在对应阶段目录下）：

```bash
python -m pytest -q                # 基线验收测试（22 用例，全离线）
python demo.py                     # 离线端到端回归（对话 → 工具 → 退出 → resume → 分叉）
python -m harness list             # CLI
python -m harness.webui.server     # 可视化页面（真实 API，http://127.0.0.1:8765/）
./run.bat                          # 双击即离线 demo；run.bat chat --fake 交互对话
```

### `_01_Prompt_Assembly`（M4 提示词装配）

```bash
cd P2_Coding/_01_Prompt_Assembly
python -m pytest -q
python -m harness run "帮我算 1234*56.78" --fake
python -m harness run "..." --dump-prompt    # 【将实现】打印装配过程与来源
python demo.py
./run.bat chat --fake
```

### `_02_Capability_Seams`（M5 能力接缝）

```bash
cd P2_Coding/_02_Capability_Seams
python -m pytest -q                          # 【将含】local / memory 双 provider 对照
python -m harness run "把 hello 写到 notes/a.txt" --fake
python demo.py
./run.bat chat --fake
```

### `_03_Profile_Composition`（M6 Profile 组装）

```bash
cd P2_Coding/_03_Profile_Composition
python -m pytest -q
python -m harness --profile dev dump-config     # 【将实现】dev 装配树
python -m harness --profile prod dump-config    # 【将实现】prod 装配树
python -m harness --profile dev run "帮我算 2+3"
./run.bat --profile dev dump-config
```

### `_04_Service`（M7 服务化）

```bash
cd P2_Coding/_04_Service
python -m pytest -q
python -m harness serve                          # 【将实现】常驻服务（stdio JSON-RPC）
python -m harness attach --session s1            # 【将实现】另一个终端：跟随会话
python -m harness attach --session s1 --from 10  # 从 seq=10 补齐
./run.bat serve
```

### 门禁（在 P2_Coding 目录下）

```bash
python scripts/check.py        # ruff（整体）+ pytest（逐阶段）
python scripts/check.py --fix  # 先让 ruff 自动修可修的问题
```

---

## 更新规则（每完成一个阶段）

1. 在该阶段目录里写实现 + 测试，**跑通该阶段的验收命令**；
2. 在它的 README 里把验收清单勾上、更新本篇进度表；
3. 写决策记录 `docs/decisions/000N-*.md`（M4→0006、M5→0007、M6→0008、M7→0009；
   0001–0005 已被 P1 占用）；
4. `python scripts/check.py` 全绿；
5. 下一个阶段的起点 = 复制本阶段完成后的 `harness/` 目录。

## 进度

| 阶段 | 主题 | 状态 | 完成日期 | 验收命令 |
|---|---|---|---|---|
| `_01_Prompt_Assembly` | M4 提示词装配 | ☐ 未开始 | | `python -m harness run --dump-prompt` |
| `_02_Capability_Seams` | M5 能力接缝 | ☐ 未开始 | | `pytest -q`（local/memory 双 provider 全绿） |
| `_03_Profile_Composition` | M6 Profile 组装 | ☐ 未开始 | | `python -m harness --profile dev dump-config` |
| `_04_Service` | M7 服务化 | ☐ 未开始 | | `python -m harness serve` + `attach` |

> **阶段名 vs 计划阶段**：目录名字是本阶段**主题**（如 `_01_Prompt_Assembly`），
> 它服务的**学习计划阶段**是 M4–M7，见各阶段 README 顶部标注。
