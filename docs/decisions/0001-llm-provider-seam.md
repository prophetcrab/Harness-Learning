# 0001 LLM 接缝：协议先行、provider 翻译方言

- 日期：2026-09-30
- 练习：P1_Coding/_01_Provider_Protocol
- 状态：已采纳

## 背景

P0 的四个脚本里，`openai` 客户端、`chat.completions.create`、`tool_calls` 的解析
散落在主循环中。后果：无法离线测试（每次测试都要真实 API）；换供应商要改主循环；
模型方言（JSON 字符串参数、tool_call_id 配对）的细节污染业务代码。

## 决策

建立三角色接缝：
- **定义**（`llm_seam/provider.py`）：`LLMProvider` 协议（`Protocol` + `runtime_checkable`），
  加中立的 `LLMRequest` / `LLMResponse`。
- **实现**：`FakeLLM`（剧本 + 请求记录，离线）与 `DeepSeekProvider`（真实，翻译方言）。
- **消费者**（`llm_seam/loop.py`）：`run_tool_loop` 只 import 协议，不 import openai。

方言翻译做成模块级纯函数（`message_to_wire` / `response_from_api` / `tool_to_wire`），
不依赖 openai 的类定义，用 `SimpleNamespace` 即可离线测试。

## 备选与排除理由

1. **直接 mock openai 客户端**。排除：测试绑定 SDK 内部结构，SDK 升级即碎；
   且 mock 无法表达"剧本顺序"这类行为契约。
2. **用抽象基类（ABC）定义接口**。排除：强迫实现者继承，增加第三方接入成本；
   `Protocol` 的结构化类型更符合"接缝要松散耦合"的目标。
3. **保留 `messages: list[dict]` 直接透传**（P0 的做法）。排除：dict 无类型约束，
   "assistant 消息必须携带 tool_calls"这条协议纪律只能靠注释提醒；dataclass 把它变成结构。
4. **先做流式**。排除：流式涉及增量帧聚合、取消、回压，会掩盖"接缝"这一主题的核心
   （定义/实现/消费者分离）。流式留到后续主题，`complete()` 的协议形状不变。

## 后果

- 约束：`llm_seam/loop.py` 永远不得 import 厂商 SDK；新增 provider 必须实现 `complete()`。
- 收益：12 个测试 0.04 秒跑完，全程离线；`demo.py` 第 3 节是唯一的 provider 构造点。
- 债务：`complete()` 是非流式的，真实交互中无法显示"逐字输出"；`request.model` 字段
  目前只在 DeepSeekProvider 里被使用，FakeLLM 忽略它。
- 后续：`_02_Agent_Loop` 在此协议上构建正式循环；流式与重试在更后面的主题补。
