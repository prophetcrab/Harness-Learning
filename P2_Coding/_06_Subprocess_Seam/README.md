# _06_Subprocess_Seam —— SubprocessService 接缝 + shell 工具

P2 第 6 阶段（对应学习计划 **M5**）。本阶段**只加一个机制**：

> 为命令执行建三角色接缝（spawn / 捕获输出），加一个 `shell` 工具——工具只依赖抽象。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0011](../../docs/decisions/0011-subprocess-seam.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_05` 完成后的
`harness/` + `prompt/` + `context/` + `providers/`，本阶段给 `providers/` 加第二条接缝）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `providers/subprocess.py`（顶层） | ★ **本阶段新增的机制** | `SubprocessService` 定义 + `CommandResult` 结果词汇 + 稳定错误码 |
| `providers/subprocess_local.py`（顶层） | ★ Provider | `LocalSubprocess`：真实子进程（整树超时终止 + 凭证剥除） |
| `providers/subprocess_scripted.py`（顶层） | ★ Provider | `ScriptedSubprocess`：剧本回放（零真实进程） |
| `providers/toolbox.py` | Consumer | ★ 新增 `shell` 工具 + `build_toolbox`（fs + shell 一起装配） |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |

## 0) 现状基线

```bash
cd P2_Coding/_06_Subprocess_Seam
python -m pytest -q        # 22 个基线用例 + 44 个本阶段用例 = 66
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/subprocess/subprocess/src/index.ts`（spawn / 捕获 / 终止整棵进程范围）
- `packages/shell/shell` + `bash-local/` + `tool-bash/`（接缝的教科书样例：callers own deadlines）

**做**：
- ✅ `SubprocessService` 定义（run / 捕获输出 / 期限）；`LocalSubprocess` 本地实现；
- ✅ `shell` 工具（超时、退出码、stdout/stderr）。

**验收**：
- [x] 捕获 stdout / stderr / 退出码（三者分开；非零退出是**结果**不是异常）
- [x] 超时能被处理（超时被杀也是结果：`timed_out=True`；**杀整棵进程树**）
- [x] 工具只依赖抽象（同一套工具测试在 LocalSubprocess / ScriptedSubprocess 下全绿）
- [x] 基线 22 用例仍全绿；门禁绿（66 用例，ruff 通过）

## 2) 本阶段新增的东西

**第二条接缝，三角色与 fs 接缝同构**：

| 角色 | 文件 | 内容 |
|---|---|---|
| **Definition** | `subprocess.py` | `SubprocessService` 协议（`run(command, cwd=, timeout=)`）+ `CommandResult` + 稳定错误码 |
| **Provider** | `subprocess_local.py` | `LocalSubprocess`：真实 `subprocess`；超时杀整树；子进程环境剔除凭证 |
| **Provider** | `subprocess_scripted.py` | `ScriptedSubprocess`：剧本队列回放 + 请求记录（FakeLLM 同款纪律） |
| **Consumer** | `toolbox.py` | `shell` 工具：只调协议；组装时解析 cwd / 期限 / 输出上限 |

**结果词汇**（本接缝的核心设计）：一次执行的结局是 `CommandResult(exit_code, stdout,
stderr, timed_out)`。**非零退出、超时被杀都是"结果"**——它们要进日志、进模型历史，
让模型读报错、改命令重试；只有**基础设施失败**（cwd 不存在、shell 起不来）才抛
`SubprocessError(SHELL_SPAWN_FAILED)`（对齐 dsh ctx.shell："only infrastructure
failures reject"）。

**工具层的三条渲染**（与文件错误同构的 `{"error", "code"}` 结构）：

| 结局 | 渲染 |
|---|---|
| 正常结束（exit 0） | `{command, exit_code, stdout, stderr}` |
| 非零退出 | `error + code=SHELL_NONZERO_EXIT` + exit_code + 双流 |
| 超时被杀 | `error + code=SHELL_TIMEOUT` + 已刷出的输出 |
| 起不来 | `error + code=SHELL_SPAWN_FAILED` |

**消费侧约束（显式解析）**：模型只给 `command`；**工作目录**（= 会话工作区）、
**期限**（默认 15 秒，组装时可配）、**输出上限**（默认 2 万字符，超出截断防日志爆炸）
都在组装时定死——对齐 dsh 的"callers own deadlines"。

**两个安全默认**：`shell` 工具标了 `needs_approval`（执行任意命令 ≥ 写文件）；
`LocalSubprocess` 启动前从子进程环境剔除 `DEEPSEEK_API_KEY` 等凭证（实测验证：

子进程里读不到 key）。

## 3) 运行方法

```bash
cd P2_Coding/_06_Subprocess_Seam
python -m pytest -q

# ★ 交互入口：--fs 与 --shell 两条接缝分别可换
python chat.py --shell local --ask "用 shell 看看当前目录"   # 真实执行（需审批）
python chat.py --shell fake --ask "用 shell 跑一下"          # 剧本回放（零真实进程）
python chat.py --fs jail --shell local                      # 文件围栏 + 真实命令
python chat.py --fake                                       # 离线：shell 自动切剧本
# 会话内命令：/fs、/shell 看当前 provider；/prompt 装配单；/exit

# 脚本化演示（离线，跑完整故事线）
python demo.py        # 0a 接缝两实现对照；0b 三种结局的渲染；0c 消费侧约束与截断
                      # 1) 主故事：真实执行一条命令（审批）→ 写文件（审批）→ resume
```

**真实对话里能看到什么**：模型说"我跑个命令看看"，审批通过后命令**真的执行**，
stdout/退出码作为工具结果回给模型（它还会用其它工具交叉验证）；拒绝审批则命令根本
不跑、模型收到拒绝原因；超时/非零退出也都有模型可读的结构化信息。

## 4) 完成后的去向

**M5 完成**（能力接缝：fs 三条实现 + subprocess 两条实现，全可互换）。
下一阶段 `_07_Plugin_Effect` 进入 **M6 组合与配置**：插件协议 `setup(ctx) -> disposer`，
把"注册即 effect、卸载自动回卷"立成正式机制。
