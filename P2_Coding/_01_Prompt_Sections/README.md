# _01_Prompt_Sections —— 提示词 section 注册表与装配

P2 第 1 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> 把「一个写死的 system_prompt 字符串」升级成**有序的 section 注册表 + 装配器**——提示词是组装出来的，不是手写的。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0006](../../docs/decisions/0006-prompt-sections.md)

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段做了什么 |
|---|---|---|
| harness/prompt/ | 占位（只有 __init__.py） | ✅ section 注册表 + 作用域遮蔽 + 装配器 + 默认 section |
| harness/mini.py | 接收 system_prompt 字符串 | ✅ `open()` 的 system_prompt 接受 `str \| PromptAssembler \| None` |
| harness/agent/loop.py | 用固定 system_prompt | 未改（提示词仍在装配点归一为文本） |

## 0) 现状基线

```bash
cd P2_Coding/_01_Prompt_Sections
python -m pytest -q        # 22 个基线用例 + 15 个本阶段用例 = 37
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
- [x] 基线 22 用例仍全绿；门禁绿（37 用例，ruff 通过）

## 2) 本阶段新增的东西

`harness/prompt/` 五个模块（纯逻辑，不依赖 harness 其它子包）：

| 文件 | 内容 |
|---|---|
| `section.py` | `Section`：一节提示词 = name + content + source + title；不可变，空名报错 |
| `registry.py` | `SectionRegistry`（基础层，有序、重名 fail loud）+ `SectionScope`（同名遮蔽/追加/回卷） |
| `assembler.py` | `PromptAssembler`：按来源顺序拼成文本，分隔符可配；`parts()` 供追溯 |
| `defaults.py` | 内置默认 section（role / tools / style）与默认装配器 |
| `__init__.py` | 包出口 |

排序规则（关键）：作用域**遮蔽一个已有名字时保持它在基础层的位置**，**新增的名字追加到末尾**；
基础层始终完整，遮蔽发生在解析期——所以 `close()`/`drop()` 后基础层自动恢复。

## 3) 运行方法

```bash
cd P2_Coding/_01_Prompt_Sections
python -m pytest -q
python demo.py                               # 第 0 节演示 section 装配与作用域回卷
python -m harness run "帮我算 1234*56.78" --fake
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 4) 完成后的去向

把「静态 section」升级为「动态上下文」：下一阶段 `_02_Prompt_Context` 加变量插值与运行时渲染
（`{{cwd}}` / `{{platform}}` / `{{time}}`，以及"每 step 渲染、结果进日志"）。
