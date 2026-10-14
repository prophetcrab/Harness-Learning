# _03_Usage_Budget —— 用量与预算（token 计量 + 超限收尾）

P3 第 3 阶段（对应 [PLAN.md](../PLAN.md)）。**本阶段只加一个机制**：

> 每次模型调用的 token 用量被**累计**（按 turn / 按会话）；可设**预算上限**，
> 超出时以结构化状态 `budget_exceeded` 收尾（不是异常）。

**状态：✅ 已完成（2026-10-14）**

## 本阶段新增/改动

| 文件 | 角色 | 内容 |
|---|---|---|
| `plugins/token_usage.py` | ★ **本阶段的机制** | `UsageLedger`（内存账本）+ `token-usage` 插件（订阅记账、`usage_stats` 工具、用量页面 + `/api/usage`） |
| `components/llm/vocabulary.py` | 词汇 | `Usage` 支持累加（`+`）与 `total_tokens` |
| `components/llm/streaming.py` | 流式 | `StreamChunk.usage`（末帧携带）+ 聚合时带回 |
| `components/llm/deepseek.py` | 真实 provider | 流式请求显式开启 `stream_options.include_usage`；`chunk_usage_from_frame`（用量在**帧**上，不在 delta 上） |
| `components/agent/loop.py` | 主循环 | turn 内累计用量；超预算 → `budget_exceeded` 收尾；发 `step_usage` / `budget_exceeded` 事件 |
| `components/agent/trace.py` | 轨迹 | `TurnResult.usage`；`TurnStatus` 增加 `budget_exceeded` |
| `components/session/events.py` + `recorder.py` | 日志 | 新增事件 `usage/step`、`budget/exceeded` —— **用量是事实，进日志** |
| `plugins/llm.py` | 装配 | 流式路径把末帧用量随 `llm/response` 广播（否则账本对流式是瞎的） |
| `plugins/boot.py` / `agent.py` / `chat.py` | 接线 | `token_budget` 参数贯通；默认装 `token-usage` 插件 |

## 四条设计要点

**1. 用量是"事实"，所以进日志。**

`usage/step`（每次调用的用量）与 `budget/exceeded`（超限）都写进会话日志——
与流式的 `step_delta`（过程）相反。理由：预算核算、事后对账、跨进程 resume 都
需要它，而且它很小（几个整数），不会让日志膨胀。

**2. 超预算是"结果"，不是异常。**

turn 以 `budget_exceeded` 收尾：部分文本保留、用量如实带回、状态可判。
调用方据此决定下一步（提醒用户、降级、停止），而不是捕获异常。
检查点放在"判定完成"**之前**——超预算的 turn 不该再假装正常收尾。

**3. 流式路径的用量要单独接。**

流式下的用量在**末帧的帧对象**上（不在 delta 上），而且 OpenAI 兼容协议
**默认不上报**——必须显式开 `stream_options.include_usage`。这两点都实测踩到过：
不开的话账本永远是 0；取错位置（从 delta 取）也永远是 0。

**4. 账本按"会话 + turn"聚合，不是只按 turn。**

turn 号是**每会话独立编号**的。只按 turn 聚合会把不同会话的同号 turn 并成一条
（实测：两个会话的 turn 1 显示成"turn 1 有 4 次调用"）。现在账本记 `(session, turn)`。

## 怎么用

```bash
cd P3_Coding/_03_Usage_Budget
python -m pytest -q                       # 158 个用例（含前两阶段）

# 真实组合（用量页面默认起在 8767）
python chat.py --web --realtime --self-tools
#   主界面顶栏出现「📊 Token 用量」按钮（服务自报地址，页面通用发现）
#   点开是 iframe 面板；也可直接访问 http://127.0.0.1:8767/

# 设预算（超出即以 budget_exceeded 收尾）
python chat.py --realtime --token-budget 50000
```

模型也能自查：`usage_stats` 工具返回总用量、按会话/turn 明细、最近 20 次调用。

## 实测（真实 DeepSeek）

```
[alpha] 用一句话说你好 → done
[beta] 帮我算 12*34 → done

账本: 3 次调用 · 合计 5866 tokens
  alpha · turn 1: 1920 tokens（1 次）
  beta  · turn 1: 3946 tokens（2 次）      ← 同号 turn 正确分开
```

## 验收（对照 PLAN.md）

- [x] 用量累计正确：一个 turn 内多次调用求和（FakeLLM 指定 usage 断言精确值）
- [x] 超限返回结构化状态（`budget_exceeded`）+ 部分文本保留 + 用量带回
- [x] 边界：恰好等于预算**不算**超；`token_budget=None` 不限；非正值 fail loud
- [x] 事件可观测：`step_usage` / `budget_exceeded` 进日志，`usage/step` 已入库
- [x] 模型可读（`usage_stats` 工具）；人可读（页面 + CORS JSON）
- [x] 与 `_02` 流式协同：流式路径的用量也能记账（含真实 API 的末帧 usage）
- [x] 门禁绿（ruff + pytest，158 用例）

## 修的三个真问题（都留了回归测试）

1. **流式默认不报用量**：不显式开 `stream_options.include_usage`，账本永远是 0
   ——请求形状有离线断言钉住。
2. **用量在帧上不在 delta 上**：取错位置同样导致 0——`chunk_usage_from_frame` 专门处理。
3. **账本把不同会话的同号 turn 混在一起**：改为按 `(session, turn)` 聚合。

## 完成后的去向

`_04_Compaction`（PLAN.md）：上下文压缩——超阈值时把旧区段摘要成一条消息；
日志用"遮蔽"实现，绝不改写历史。用量数据正好是压缩阈值的输入（"上下文快满了"）。

---

> 目录说明：`_03` 是开发位置（从 `_02` 完成态复制）；`mini_harness/` 是基线快照。
> 阶段定位与路线见 [PLAN.md](../PLAN.md) 与 [../README.md](../README.md)。
