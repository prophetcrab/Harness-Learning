# _01_Prompt_Sections —— 提示词 section 注册表与装配

P2 第 1 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> 把「一个写死的 system_prompt 字符串」升级成**有序的 section 注册表 + 装配器**——提示词是组装出来的，不是手写的。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0006](../../docs/decisions/0006-prompt-sections.md)

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——各阶段共享、本阶段不动它；
> 本阶段**新增的机制放在阶段主目录的顶层**，与 `harness/` 平级。所以本阶段的产物是顶层
> 的 `prompt/` 包，`harness/` 一行未改。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `prompt/`（顶层） | ★ **本阶段新增的机制** | section 注册表 + 作用域遮蔽 + 装配器 + 默认 section |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |
| `demo.py` / `tests/` | 入口与测试 | 装配点：`assembler.assemble()` → `MiniHarness.open(system_prompt=...)` |

## 0) 现状基线

```bash
cd P2_Coding/_01_Prompt_Sections
python -m pytest -q        # 22 个基线用例 + 19 个本阶段用例 = 41
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/core/system-prompt/src/index.ts`（section 注册与渲染）
- `docs/subsystems/system-prompt.md`

**做**：
- ✅ `harness/prompt/`：**有序** section 注册表，支持**作用域覆盖**（呼应铁律 #4「注册表分层 + 遮蔽」）；
- ✅ 装配器：按注册顺序把 section 拼成完整 system 提示词；
- ✅ 快照测试：同一组 section 渲染两次结果一致。

**验收**：
- [x] section 按注册顺序装配
- [x] 新增/删除一个 section 不影响其它 section
- [x] 作用域覆盖能遮蔽同名 section（保持原位；撤下后基础层恢复）
- [x] 基线 22 用例仍全绿；门禁绿（41 用例，ruff 通过）

## 2) 本阶段新增的东西

**顶层 `prompt/` 包**（纯逻辑，不依赖 harness；只吃字符串、吐字符串）：

| 文件 | 内容 |
|---|---|
| `section.py` | `Section`：一节提示词 = name + content + source + title；不可变，空名报错 |
| `registry.py` | `SectionRegistry`（基础层，有序、重名 fail loud）+ `SectionScope`（同名遮蔽/追加/回卷） |
| `assembler.py` | `PromptAssembler`：按来源顺序拼成文本，分隔符可配；`parts()` 供追溯 |
| `defaults.py` | 内置默认 section（role / tools / style）与默认装配器 |
| `__init__.py` | 包出口 |

排序规则（关键）：作用域**遮蔽一个已有名字时保持它在基础层的位置**，**新增的名字追加到末尾**；
基础层始终完整，遮蔽发生在解析期——所以 `close()`/`drop()` 后基础层自动恢复。

接入方式：装配器产出**一段文本**，交给基线的 `harness.mini.MiniHarness.open(system_prompt=...)`
（基线接口未改）。装配点是本阶段的 `chat.py`（真实对话）与 `demo.py`（脚本演示）。

## 3) 运行方法

```bash
cd P2_Coding/_01_Prompt_Sections
python -m pytest -q

# ★ 真实对话（本阶段入口）：启动时打印装配明细，把装配出的提示词用于整场对话
python chat.py                 # 真实 DeepSeek API（读取项目根 .env 的 key）
python chat.py --fake          # 离线剧本对话（不需要 key）
python chat.py --session s1    # 指定会话，同名即"恢复继续"
python chat.py --no-scope      # 只看基础三节，不叠加 chat 作用域
# 对话内命令：/prompt 看装配明细；/history 看历史；/exit 退出

# 脚本化演示（离线，跑完整故事线）
python demo.py                 # 第 0 节演示 section 装配与作用域回卷
python -m harness run "帮我算 1234*56.78" --fake
./run.bat                      # 双击即离线 demo
./run.bat chat                 # 一键真实对话；run.bat chat --fake 离线对话
```

**真实对话里能看到什么**：`chat.py` 启动时逐节打印系统提示词（name / 来源 / 内容），
叠加 `chat` 作用域后追加一节 `conversation`；随后这场对话用的就是这段"装配出来的"
提示词（它会被写进会话日志的 `session/start`，即"模型当时看到的提示词"）。

## 4) 完成后的去向

把「静态 section」升级为「动态上下文」：下一阶段 `_02_Prompt_Context` 加变量插值与运行时渲染
（`{{cwd}}` / `{{platform}}` / `{{time}}`，以及"每 step 渲染、结果进日志"）。
