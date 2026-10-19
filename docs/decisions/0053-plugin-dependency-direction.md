# 0053 插件间依赖方向：只允许由外向内，兄弟能力不互相 import

- 日期：2026-10-19
- 阶段：P4_Coding（M2 收尾 / 灾难恢复后）
- 状态：已采纳；收紧 [0052](0052-remove-components-layer.md) 的"类型可以随便 import"

## 背景

去掉 `components/` 之后（0052），共享类型按功能域并进了插件模块，纪律放宽成
"类型与纯函数允许插件间直接 import，只要求无环"。

恢复被批处理脚本破坏的能力插件时，这条宽口径暴露出问题：

- 同一个类型在多个插件里各有一份**副本**（如 `Usage` / `ToolResult`），
  有些副本被脚本改坏，字段和主版本悄悄漂移，测试才发现；
- 另一些地方又直接 import 基座模块的类型。同一类跨插件数据，一会儿"复用"、
  一会儿"复制"，没有可执行的判据，只能靠人记。

用户的原则是：**插件之间最小耦合，所有依赖必须明确且谨慎，不怕代码重复**。
"随便 import、只要不成环"不够贴这条原则。

## 决策

**插件间 import 只允许由外向内，且兄弟能力之间禁止互相 import。**

| 来源 | 允许 import | 禁止 import |
|---|---|---|
| `plugins/base/*` | 标准库、`core` | 任何 capability / application |
| `plugins/capability/*` | 标准库、`core`、**base 插件的类型 / 纯函数** | 其它 capability（含同功能域的兄弟模块） |
| `plugins/application/*` | 任意（组合根） | —— |

行为协作仍然只有**服务与事件**两条通道；这条规则只约束类型层。

### 为什么 capability 可以 import base

有两个地方"身份一致"是硬需求，复制会直接制造 bug：

1. **LLM 分片的 `isinstance`**：`Finish` / `Usage` / `TextDelta` 是流式消费方
   按类判别的数据，fake 适配器与真实适配器必须产出与 `llm` 服务同一身份的对象；
2. **会话事件与工具定义**：`SessionEvent` / `ToolDefinition` 在跨插件传递时
   如果各留一份，`isinstance` 与字段校验会出现"看着一样但不相等"的怪事。

所以这些类型由 base 提供，capability 单向 import。反过来 base 永远不认识
具体能力——这是依赖倒置的底线。

### 为什么兄弟能力必须零 import

兄弟之间没有"谁更稳定"之分：一旦 `tool-fs` import `tool-shell`，两者就
只能在同一个装配里同生共死，热插拔和替换实现都会受牵连。它们的协作应当
走服务（`tool-admin`、`fs`、`proc`）或事件，实在需要共享数据结构就各留一份
副本——**重复是显式成本，隐式耦合不是**。

## 备选与排除理由

**A. 维持 0052 的"无环即可"**。实现上最省事，但恢复期间已经证明它无法
阻止类型副本漂移，也无法回答"这两个插件到底能不能一起拆"。放弃。

**B. 所有插件零 import，全部用鸭子类型 + 副本**。最彻底，但流式分片与
会话事件的 `isinstance` 语义会丢；测试要么改成鸭子判别（削弱验收），
要么在每个适配器里维护一份永远不能漂移的副本（比 import 更脆）。放弃。

**C. 恢复 `components/` 作为中立类型层**。用户已经明确否掉过这个目录
（0052 背景），不再重启。放弃。

## 后果

- `tests/test_purity.py` 新增 `test_plugin_imports_go_one_way`：AST 扫描
  顶层 import，按角色目录判定方向，违规直接失败；
- 目前全项目只有 4 条跨插件 import，全部是 `capability → base`：
  `llm/fake.py`、`llm/provider.py`、`llm/retry.py`、`llm/tokens_usages.py`
  从 `plugins.base.llm` 取类型与协议；
- 其余能力插件（fs 策略、tools、proc、agent、prompt、session、workspace）
  保持自包含：类型各留一份副本，协作只认服务名 / 事件名 / 鸭子类型；
- 486 用例全绿，无环检查继续保留。

## 相关

- [0052](0052-remove-components-layer.md)（删掉 components、放宽为无环）、
  [0042](0042-p4-base-boundary.md)（基座边界）；
- 用户原话："整个项目的设计应该遵循不同插件之间的最小耦合原则，所有相互的
  依赖和耦合都必须明确且谨慎，不需要怕代码重复"。
