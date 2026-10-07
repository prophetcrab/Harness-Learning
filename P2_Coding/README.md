# P2_Coding —— 把 harness 长成一个真正的产品

P1 结束时你有一个"能对话、能恢复、可扩展工具、可离线测试"的小 harness，但它是
**四份自包含副本拼起来的**，组装发生在练习目录里。P2 要做的是：**先把它收敛成一个
真正的包，再在这个包上把 harness 变成可配置、可替换、可服务化的产品。**

对应学习计划 [docs/learning-plan.md](../docs/learning-plan.md) 的
**M4（提示词装配）→ M5（能力接缝）→ M6（Profile 组装）→ M7（服务化）**。

---

## 第 0 步：地基收敛（已完成）

这一步是 P1 留下的欠账，也是 M4–M7 的共同前提——**不先做，后面每加一个能力都要复制多份**：

1. **四份副本 → 单一 `harness/` 包**。P1 `_05` 里各带一份的 `llm_seam` / `agent_loop` /
   `tool_pipeline` / `session` 收敛成 `harness.llm` / `harness.agent` / `harness.tools` /
   `harness.session`；原来的 `conftest.py` 驱逐模块技巧随之作废（不再有同名包）。
2. **补上 M0 门禁**：`pyproject.toml` + `scripts/check.py`（ruff + pytest；mypy 可选）。
   对应学习计划硬约束 #3「门禁不绿不前进」。

**验收**：`python scripts/check.py` 全绿；`python -m harness list` 可用；P2 22 个用例通过
（与 P1 `_05` 迁移前的行为完全一致）。

---

## 目标仓库结构

```
P2_Coding/
├── README.md                  ← 本文件（P2 学习计划）
├── pyproject.toml             ← 打包 + ruff/pytest 配置
├── scripts/check.py           ← 门禁：ruff + pytest（+ mypy 若装了）
├── harness/                   ← 实现代码（单一包）
│   ├── __init__.py / __main__.py  ← python -m harness
│   ├── llm/                   ← M1 产出：LLM 接缝（协议/词汇/Fake/DeepSeek）
│   ├── agent/                 ← M1 产出：主循环（turn/step + 轨迹 + 取消）
│   ├── tools/                 ← M2 产出：注册表 + 审批 + pre/execute/post 管线
│   ├── session/               ← M3 产出：事件日志 + JSONL + resume
│   ├── mini.py / runner.py    ← 装配：MiniHarness / Runner
│   ├── env.py / cli.py        ← provider 构造 / 命令行入口
│   ├── webui/                 ← 本地可视化页面（stdlib http.server）
│   ├── prompt/                ← M4：系统提示与上下文装配（待实现）
│   ├── providers/             ← M5：能力接缝（FileSystem / SubprocessService）
│   ├── config/                ← M6：profile 式组装
│   └── server/                ← M7：JSON-RPC 服务 + 事件流 follow
├── profiles/                  ← M6：YAML 组合层
├── tests/                     ← 单元测试（test_harness_assembly / test_harness_webui）
└── demo.py                    ← 离线端到端回归（M1–M3 的行为基线）
```

`harness/prompt`、`harness/providers`、`harness/config`、`harness/server` 现在是**占位包**
（只有说明性的 `__init__.py`），每个阶段往对应目录里写实现。

---

## 阶段计划

### M4 系统提示与上下文装配（3–4 天）

**目标**：提示词是可组合、可追溯、可重建的产物——不再是散落在各处的字符串拼接。

**读**：`dsh/packages/core/system-prompt/src/index.ts`；`dsh/packages/context/`
（workspace 指令、时间上下文）；`dsh/docs/subsystems/system-prompt.md`。

**做**：
- `harness/prompt/`：section 注册表（有序、支持作用域覆盖）；
- 变量插值 `{{cwd}}` / `{{platform}}` / `{{time}}`；
- 运行时上下文每 step 渲染，`system/message` 进日志；
- 装配断言：渲染出的提示词必须能由「日志 + 装配器」重建；
- CLI：`python -m harness run --dump-prompt` 打印装配过程与来源。

**验收**：快照测试稳定；改 cwd 只影响对应 section；新增一个 section 不动其他部分。

**决策记录**：`docs/decisions/0006-prompt-assembly.md`。

### M5 能力接缝：Provider 替换（4–6 天）

**目标**：把 M2 里"能用"的文件/命令工具重构成三角色接缝，体验"换 provider 换产品"。

**读**：`dsh/packages/shell/shell/src/index.ts` + `bash-local/` + `tool-bash/`
（接缝的教科书样例）；`dsh/packages/fs/fs/src/index.ts`；
`dsh/packages/subprocess/subprocess/src/index.ts`；`dsh/docs/capability-seams.md`。

**做**：
- `harness/providers/`：抽象 `FileSystem`（read/write/edit/列表）与
  `SubprocessService`（spawn/捕获）；本地实现；工具只依赖抽象；
- 单槽服务：重复注册即报错；provider 选择在**显式 resolve 步骤**完成；
- 至少两个新 provider：`MemoryFS`（测试用）、`WorkspaceJailFS`（越出工作目录的写被拒）。

**验收**：同一套工具测试在 `local` 与 `memory` 下全绿；jail 越权错误结构与其他错误一致。
（此阶段会动 `harness/tools/` 的 `read_file`/`write_file`/`list_files`——它们从直接操作
`pathlib` 改为依赖 `FileSystem` 抽象。）

**决策记录**：`docs/decisions/0007-capability-seams.md`。

### M6 组合与配置：Profile 式组装（3–5 天）

**目标**：能力组合从代码变成配置数据；一行 patch 完成 provider 替换。

**读**：`dsh/packages/boot/app-boot/src/profile.ts`；
`dsh/packages/bundle/base/cordis.patch.yml`；`dsh/apps/cli/src/profile-boot.ts`；
`dsh/docs/cordis-primer.md`（loader configuration 节）。

**做**：
- `harness/config/`：极简插件协议 `Plugin.setup(ctx) -> disposer`；注册即 effect；
- `profiles/*.yaml`：有序层 + 按 id patch（替换整块 config 或 insert 新行）；
  顺序 base → profile patch → 用户 patch → CLI `--patch`；
- `python -m harness --profile <name> dump-config`；
- 两个 profile：`dev`（FakeLLM + MemoryFS）、`prod`（DeepSeek + 本地）。

**验收**：同 base 两个 profile 产出不同且可读的树；换 LLM provider 只改一行；
故意写错配置启动即报错并指出位置。

**决策记录**：`docs/decisions/0008-profile-composition.md`。

### M7 服务化与多前端（5–7 天）

**目标**：harness 成为常驻服务；前端通过事件流跟随会话（断线可补）。

**读**：`dsh/packages/sdk/protocol/src/transport.ts`（换行 JSON-RPC）；
`dsh/packages/api/gateway/src/index.ts`（RPC 网关）；
`dsh/packages/host/webserver/src/index.ts`；`dsh/packages/client/connection/`；
`dsh/docs/api-gateway.md`。

**做**：
- `harness/server/`：`python -m harness serve`，先做 stdio 换行 JSON-RPC
  （`initialize` / `session.prompt` / `session.follow`），可选升级 HTTP + WebSocket；
- `session.follow(from_seq)` = **重放（日志）+ 订阅（实时）**；
- 客户端 `python -m harness attach`：流式渲染（rich 可选）。

**验收**：两个终端 serve + attach 共享同一会话；kill attach 后重连能补齐缺失事件；
协议 golden 测试。

**决策记录**：`docs/decisions/0009-follow-equals-replay-plus-subscribe.md`。

---

## 怎么用这个工作区

```bash
cd P2_Coding

# 门禁（每次动手前/收工前都跑）
python scripts/check.py            # ruff + pytest（+ mypy 若装了）
python scripts/check.py --fix      # 先让 ruff 自动修可修的问题

# 测试
python -m pytest -q                # 22 个用例（组装 13 + 可视化 9）

# 离线端到端回归（M1–M3 的行为基线，改任何底层后都该跑）
python demo.py

# 命令行
python -m harness list
python -m harness run "帮我算 1234*56.78" --fake
python -m harness chat --session s1        # 真实 API（读取项目根 .env）

# 可视化页面（真实 API）
python -m harness.webui.server             # http://127.0.0.1:8765/
```

---

## 与 P1 的关系

- **行为不变**：P2 的 `harness/` 是 P1 `_05` 四份副本的**语义等价合并**，`demo.py` 与
  22 个测试即回归基线（迁移前后全部通过）。
- **结构升级**：不再有"每个练习一份同名包"的重复；`python -m harness` 成为统一入口。
- **P1 `_05` 保留**：作为"组装成产品"那一步的教学快照存在，其代码与 P2 内容重合，
  后续以 P2 为唯一维护对象。

## 进度

| 阶段 | 主题 | 状态 | 完成日期 | 验收命令 |
|---|---|---|---|---|
| 第 0 步 | 地基收敛（单包 + 门禁） | ✅ 已完成 | 2026-10-08 | `python scripts/check.py` |
| M4 | 提示词装配 | ☐ 未开始 | | `python -m harness run --dump-prompt` |
| M5 | 能力接缝 | ☐ 未开始 | | `pytest -q`（local/memory 双 provider 全绿） |
| M6 | Profile 组装 | ☐ 未开始 | | `python -m harness --profile dev dump-config` |
| M7 | 服务化 | ☐ 未开始 | | `python -m harness serve` + `attach` |

每完成一个阶段：更新本表 → 写决策记录 `000N-*.md` → 门禁全绿 → commit。
