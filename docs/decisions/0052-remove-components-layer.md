# 0052 去掉 components 层：共享类型按功能域并入插件模块

- 日期：2026-10-16
- 阶段：P4_Coding（M2 收尾）
- 状态：已采纳

## 背景

用户质疑：**"为什么还有 components？我让你这么做了吗？"**

核对后确认：`components/` 不在用户给的架构里（不是三类插件、也不是 core 或
launcher），是我沿用 P3 的三层形状顺手加进去的，而且没有单独决策记录背书。

## 决策

**删掉 `components/`（3,465 行），按功能域并入对应的插件模块。**

| 原 `components/` | 并入 |
|---|---|
| `llm/provider.py`、`llm/wire.py` | `plugins/base/llm.py` |
| `llm/fake.py` | `plugins/capability/llm/fake.py` |
| `session/store.py` | `plugins/base/sessions.py` |
| `session/jsonl.py` | `plugins/capability/session/store_jsonl.py` |
| `tools/definition.py`、`registry.py`、`pipeline.py` | `plugins/base/tools_admin.py` |
| `prompt/assembler.py` | `plugins/base/prompt.py` |
| `proc/protocol.py`、`proc/local.py` | `plugins/base/proc.py` |
| `fs/sandbox.py`、`fs/observation.py` | `plugins/base/fs.py` |
| `agent/types.py` | `plugins/base/agent.py` |

### 放宽的纪律（如实记）

原来有一条测试：**"插件之间只有服务与事件两条通道"**——现在放宽成：

    · 行为协作：仍然只有服务与事件两条通道；
    · **类型与纯函数：允许插件之间直接 import**；
    · 唯一硬约束：**不能形成环形 import**（新测试 `test_plugin_type_imports_do_not_form_cycles`
      用 DFS 检环）。

## 备选与排除理由

**A. 保留 components（补一份决策记录背书）**。技术上更干净：共享类型有中立
位置、能脱离 harness 单测、插件间零代码依赖。**但这是用户明确否掉的选项**——
架构里没这一层，就不该由我加。

**B. 塞进 core**。否决：core 不认识 harness 词汇（llm / tool / session），
`ToolDefinition` / `SessionEvent` 进去就破了这条纯度。

**C. 改成 `shared/` 之类的名字**。否决：只换名字，"计划外的层"这个问题还在。

## 代价（如实记）

1. **插件间出现类型 import**：`tool-math` 要从 `plugins.base.tools_admin`
   import `tool` / `ToolResult`。这是"插件之间只有服务与事件"这条纪律的**实质
   性放宽**，靠"无环"测试兜底，而不是靠分层隔开。
2. **单测要装插件了**：原来 `test_session_persistence.py` 可以直接测
   `SessionReader`，现在它是 `plugins/capability/session/store_jsonl.py` 里的
   类，虽然仍可直接 import，但语义上已经属于插件模块。
3. **模块变大**：`plugins/base/tools_admin.py` 从 ~300 行涨到 ~900 行，
   `plugins/base/llm.py` 从 ~300 涨到 ~800 行。大文件的可读性靠分节注释
   （`# === 以下合并自 … ===`）维持。

## 后果

- 项目只剩 **core / plugins / 入口脚本** 三层（测试与装配单不算产品层）；
- 纯度测试改成两条：**"不许再有 components 目录"** + **"插件间 import 无环"**；
- 486 用例全绿；真实 DeepSeek API 回归通过（模型自主调 `calc` 得到 56）。

## 相关

- [0042](0042-p4-base-boundary.md)（基座边界）、
  [0043](0043-p4-rewrite-from-scratch.md)（全部重写）；
- 用户原话："我让你这么做了吗？"
