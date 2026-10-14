# _02_Streaming —— 流式输出（增量到达 + 取消）

P3 第 2 阶段（对应 [PLAN.md](../PLAN.md)）。**本阶段只加一个机制**：

> 模型回复从"一次性返回"升级为**逐段到达**：provider 出增量、循环转发
> `step_delta` 事件、界面逐字渲染；中途可取消，已收到的部分保留。

**状态：✅ 已完成（2026-10-14）**

## 本阶段新增/改动

| 文件 | 角色 | 内容 |
|---|---|---|
| `components/llm/streaming.py` | ★ **本阶段的机制** | `StreamChunk`（增量词汇）+ `ScriptedStream`（流式剧本）+ `aggregate_chunks`（聚合器，纯函数）+ `chunks_from_response`（一次性→增量） |
| `components/llm/provider.py` | 协议 | 新增**可选**协议 `StreamingLLMProvider`（只有 `stream()` 的实现才满足） |
| `components/llm/fake.py` | 离线 provider | `stream()` + 两个剧本积木：`streamed_text`（按字数切片）、`streamed_tool_call`（参数分片） |
| `components/llm/deepseek.py` | 真实 provider | `stream()`（SSE 帧 → 中立增量）+ `chunk_from_delta`（方言翻译，纯函数） |
| `components/agent/loop.py` | 主循环 | `_complete_or_stream`：有 `stream` 就逐段转发 `step_delta`，最后聚合；取消（`cancel()` / `should_stop`）在增量间生效 |
| `plugins/llm.py` | 装配 | 代理转发 `llm/stream` 事件；**诚实代理**（内层不支持就不暴露 `stream`） |
| `plugins/webui.py` | 传输 | HTTP/1.1 + **chunked** 传输 + 每帧 flush（真流式的前提） |
| `webui/index.html` | 界面 | `step_delta` → 增量气泡（带闪烁光标）；`step_response` 定稿为正式 Markdown |

## 三条设计要点

**1. 流式是"可选能力"，不是改协议。**
没有把 `complete()` 改成流式签名（那会打断所有既有实现与测试），而是在旁边加一个
`StreamingLLMProvider` 协议。循环用 `hasattr(provider, "stream")` 探测，支持就走流式、
不支持就退回一次性——**同一份剧本（`FakeLLM`）两种模式都能跑**，这保证了"流式与非流式
语义一致"是可断言的性质。

**2. 聚合是纯函数，语义只有一个。**
`aggregate_chunks(chunks) -> LLMResponse`：文本顺序拼接、工具调用按 `tool_index` 分组
并把 JSON 字符串碎片拼完再解析、`finish_reason` 取末值。参数拼完不合法就 fail loud——
宁可炸这一步，也不把半个工具调用塞给工具层。

**3. 增量是"过程"，不是"事实"。**
`step_delta` / `stream_cancelled` 是**瞬态事件**：只给观察者（界面、RPC 订阅者），
**不进会话日志**。日志里记的仍是聚合后的 `assistant/message`——所以日志体积不受流式
影响，投影无需变，重放仍然同构（`SessionRecorder._TRANSIENT_EVENTS`）。

## 实测（真实 DeepSeek）

```
增量数 101 个，首片 11.2s → 末片 11.7s，跨度 0.52s
Transfer-Encoding: chunked
逐片拼接 == 最终回答：True
```

首片延迟是模型的思考时间（要等第一个 token）；一旦开始输出，吐字很快——这是
`deepseek-flash` 的真实行为，不是实现问题。

## 验收（对照 PLAN.md）

- [x] FakeLLM 剧本支持 chunk 帧（`streamed_text` / `streamed_tool_call`）
- [x] 同一套循环测试在流式/非流式下结果一致（状态、文本、步数、工具调用）
- [x] 界面逐字显示（增量气泡 + 光标；node 逻辑验证：长度 1→2→4→6 递增）
- [x] 中途取消：结构化 `cancelled` + 保留已收文本 + 不执行半截工具调用
- [x] 无 `stream` 的 provider 自动退回（诚实代理，探测接口不撒谎）
- [x] 会话日志不含瞬态事件；基线用例仍全绿；门禁绿（ruff + pytest，131 用例）

## 修的三个真问题（都留了回归测试）

1. **包装器谎报流式能力**：每步渲染包装与 llm 插件代理曾"总是暴露 `stream`"，
   导致只有 `complete()` 的 provider 被当流式调用直接炸。改为按内层能力决定是否暴露。
2. **逐字效果退化成"一次全到"**：HTTP/1.0 下没有 Content-Length 也没有 chunked，
   客户端只能等连接关闭才认账——改为 HTTP/1.1 + 显式 chunked + 每帧 flush + 收尾终止块。
3. **测试里 `isinstance` 假阴性**：各目录带同名包副本、conftest 会驱逐模块缓存，
   实现与测试持有的 `StreamedStream` 可能是两个类对象——改用鸭子类型判定。

## 完成后的去向

`_03_Usage_Budget`（PLAN.md）：用量与预算——累计 token 用量（按 turn / 按会话）、
预算上限、超限结构化收尾。流式路径下 usage 在末帧（`LLMResponse.usage` 已预留）。

---

> 目录说明：`_02` 是开发位置（从 `_01` 完成态复制）；`mini_harness/` 是基线快照。
> 阶段定位与路线见 [PLAN.md](../PLAN.md) 与 [../README.md](../README.md)。
