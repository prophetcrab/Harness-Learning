# P1_Coding

P1 阶段工作区：**从"跑通机制"到"装进结构"**。

P0 用单文件脚本把 harness 的核心机制逐个摸了一遍（模型调用、提示词拼装、工具调用闭环、文件工具沙箱与审批）。
P1 把这些机制升级成最小 harness 的骨架：**协议化、管线化、事件化**——结束时你将拥有一个
"能对话、能恢复、可扩展工具、可离线测试"的小 harness。

对应学习计划：[docs/learning-plan.md](../docs/learning-plan.md) 的
M1（最小 agent loop）→ M2（工具系统）→ M3（会话日志）。

## 与 P0 的约定差异

1. **结构升级**：练习仍有可直接运行的入口（`python xxx.py` + `run.bat`/`run.sh`），
   但内部按需分模块；协议/类/纯函数必须能被 `import`，不再全部塞在一个文件里。
2. **测试升级**：从黑盒 subprocess 升级为可导入的单元测试——模型调用一律走 `FakeLLM`
   离线断言（剧本 + 请求记录），真实 API 只用于 demo。
3. **决策记录**：从 P1 开始，每个练习完成时写一份 `docs/decisions/NNNN-*.md`（背景/决策/备选/后果）。
   P0 阶段豁免了这条，格式见 [docs/decisions/README.md](../docs/decisions/README.md)。
4. **沿用 P0**：四件套（入口脚本 + README + 测试 + 一键启动器）、先定验收再实现、中文注释、`.bat` 纯 ASCII。

## 练习路线图

| 编号 | 主题 | 核心交付 | 验收预告 |
|---|---|---|---|
| `_01_Provider_Protocol` | LLM 接缝 | `LLMProvider` 协议 + `FakeLLM`（剧本、请求记录）+ `DeepSeekProvider`；消息词汇模块 | FakeLLM 驱动完整工具闭环，全程离线可断言；换 provider 只改一行 |
| `_02_Agent_Loop` | Agent 主循环 | 循环抽成可复用类：turn/step 词汇、max_steps、流式回调、调用轨迹返回 | 剧本覆盖"两步收尾 / 触顶停止 / 工具报错恢复"；决策记录 0001 |
| `_03_Tool_Pipeline` | 工具管线 | 注册表（define_tool 定义与实现分离）+ pre/execute/post 三段 + 超时 + 审批策略接口化 | 审批三态、超时、post 加工三类测试；决策记录 0002 |
| `_04_Session_Log` | 会话日志 | append-only 事件 + JSONL 落盘 + `derive_messages()` + resume + 崩溃尾部修复 | 重放同构、进程被杀后恢复、坏尾自动修复；决策记录 0003 |
| `_05_Mini_Harness` | 综合验收 | 前四者组装：CLI 对话 + resume + 工具箱（计算器/文件/可选搜索） | M1–M3 验收全绿；完整 demo：对话 → 工具 → 退出 → resume 继续 |

依赖关系：`_01 → _02 → {_03, _04} → _05`（_03 与 _04 可在 _02 完成后并行，也可按编号顺序做）。

### 每个练习的"读 dsh"清单（写代码前先读）

| 练习 | 先读 dsh 的 |
|---|---|
| `_01` | `packages/llm/llm/src/index.ts`（`LlmAdapter` 抽象）、`message.ts`（消息词汇） |
| `_02` | `packages/core/agent-loop/src/agent.ts`（turn/step 主流程）、`inbox.ts` |
| `_03` | `packages/core/tools/src/index.ts`（pre/guard/execute/post 管线）、`docs/tool-execution-pipeline.md` |
| `_04` | `packages/core/session/src/types.ts`（事件表）、`index.ts`（append/校验）、`session-persistence-jsonl`（落盘与代际） |
| `_05` | `packages/bundle/base/cordis.patch.yml`（组装视角）、`apps/cli/src/bin.ts` |

## 开工前建议（补 M0 欠账，可选）

学习计划 M0 的工程动作在 P0 阶段简化跳过了，P1 是补上的好时机：

- `git init` + 首次提交——目前所有练习都没有版本历史（`.gitignore` 已备好，`.env` 不会入库）；
- `pyproject.toml` + `scripts/check.py` 门禁（ruff + mypy + pytest），对应"门禁不绿不前进"。

不做也不影响练习；想做的话说一声，我来搭。

## 进度

| 练习 | 状态 | 完成日期 | 验收命令 |
|---|---|---|---|
| `_01_Provider_Protocol` | ✅ 已完成 | 2026-09-30 | `pytest _01_Provider_Protocol -q`（12 用例全离线） |
| `_02_Agent_Loop` | ☐ 未开始 | | |
| `_03_Tool_Pipeline` | ☐ 未开始 | | |
| `_04_Session_Log` | ☐ 未开始 | | |
| `_05_Mini_Harness` | ☐ 未开始 | | |

**`_01` 产出速览**：`llm_seam/` 包 6 个模块（词汇 → 协议 → FakeLLM → DeepSeekProvider → 闭环 → 工具箱），
入口 `demo.py`（`--fake` 离线 / 默认真实 API），12 个单元测试；决策记录
[docs/decisions/0001](../docs/decisions/0001-llm-provider-seam.md)。

## 运行约定

与 P0 相同：用 `.venv` 里的 Python 直接运行各练习目录的入口脚本，每个练习配 `run.bat` / `run.sh`。
环境配置与常见问题见[项目根 README](../README.md)。
