# 0044 LLM 是路由器：模型选择是数据，不是装配

- 日期：2026-10-16
- 阶段：P4_Coding（M2 第一步）
- 状态：已采纳

## 背景

需求（用户原话）：

> 对于不同 sessions，要考虑选择不同 llm 模型的问题，包括同一个 sessions 的
> 子代理，也要考虑调用不同模型的问题。
>
> provider / llm 能力插件：定位 core 不可卸载；需要支持会话选择和修改 LLM API
> 的调用配置，同时需要满足子代理可以选择不同模型的需求。

如果按"一个插件 = 一个模型"或"模型是 llm 插件的构造参数"来做，上面两条都做不到：
换模型要改装配、开新会话要用装配时的那个模型、子代理无法覆盖。

## 决策

### 一、`llm` 插件是**路由器 + 注册表**，不含任何 provider 协议代码

```python
ctx.llm.register(route, provider)          # 适配器插件注册路由（返回 disposer）
ctx.llm.stream(request)                    # 按 request.provider 分发
ctx.llm.resolve(provider, model)           # → ModelInfo（窗口 / 输出上限 / 推理档位）
ctx.llm.list_providers()
```

- 路由名是唯一键；重复注册 fail loud（不静默覆盖）；
- `llm` 的插件条目声明 `role="base"`（定位：不可卸载）。**role 是装配单的数据**
  （0042），"base 不给卸载按钮"的管理策略由 M3 的 plugin-admin 执行；
- 适配器是**能力插件**（`llm-deepseek` 等）：装卸它产品形态不变，只是少一种接入。

### 二、模型选择是**请求数据**，三层覆盖

```
装配单 defaults（新会话的默认 provider/model）
    └─ 会话：session/start 记录当时选定的 {provider, model, 调用参数}
        └─ 请求：每次请求可再覆盖（子代理、临时切换、压缩摘要用便宜模型）
```

- 会话记录进日志：恢复会话用**它自己的**模型，而不是启动时的默认值——
  否则"昨天的会话今天静默换模型"且历史里查不到；
- 子代理：`agentOptions` 叠在父级最近一次请求之上；只换路由、不显式给
  reasoning_effort 时**清掉继承值**，让新模型用自己的默认档（dsh 的规则，
  避免把 A 模型的档位塞给 B 模型）；
- 请求级可改的调用配置：`max_tokens` / `reasoning_effort` / `temperature`。
  连接类配置（base_url / key / timeout）属于**适配器插件的 config**——改它 =
  `loader.update` 换一个新适配器实例（热插拔，M1 已具备）。

### 三、上下文窗口按**解析结果**取，不写死

`context-meter` / `compact` 每次用时现查 `resolve(provider, model).context_window`。
不同模型的窗口可能差一个数量级（128K vs 1M），写死会在切模型时出静默错误。

### 四、消息内容是多模态的（图片能力保留）

```python
Message(role="user", content="纯文本")                       # 字符串，最常见
Message(role="user", content=(TextPart("这是什么？"),
                              ImagePart(url="https://…/cat.png")))   # 内容块
Message(role="user", content=(ImagePart(data=png_bytes)))     # 原始字节（适配器编 data URL）
```

- **图片是模型能力**（`ModelInfo.supports_images`）：不收图的模型在**发请求之前**
  被 `InvalidRequestError` 拒掉——别等服务端报错，那既浪费一次调用，也把
  "模型不支持"变成不透明的协议错误；
- 方言层把内容块翻成 OpenAI 兼容的多模态数组（`text` / `image_url`），
  `ImagePart.detail` 透传（low / high / auto）；
- 假 provider 的用量粗估把图片按固定值折算（`IMAGE_TOKEN_ESTIMATE`），
  统计不会漏计图片输入；
- **纯文本路径不变**：`content` 是字符串时方言仍给字符串，旧代码与旧测试
  一行不用改。

### 五、工具调用（2026-10-16 补）

    LLMRequest.tools       tuple[ToolSpec]（模型可见的工具；空 = 不带工具）
    LLMRequest.tool_choice None / "auto" / "none" / "required"
    Message.tool_calls     assistant 消息里的工具调用（ToolCall：id / name / arguments）
    Message.tool_call_id   tool 消息回报的是哪一次调用

**方言层**（OpenAI 兼容）：

- 工具声明 → `{"type": "function", "function": {name, description, parameters}}`，
  我们的扁平参数表翻成 JSON Schema 的 object schema；
- assistant 的 `tool_calls` → `[{id, type: "function", function: {name, arguments}}]`；
  没有文本时 `content` 给 `null`（接口要求）；
- tool 消息带 `tool_call_id`；
- 流式分片 → `ToolCallsDelta`（原样透出），由 `ToolCallAccumulator` 按 `index`
  拼接 id / name / arguments（参数常被切成多段，这是最容易做错的一处）。

**路由层校验**：

- 模型 `supports_tools=False` 时带工具 → `InvalidRequestError`（发请求前拒掉）；
- `tool_choice` 非法、或没有 tools 却给了 tool_choice → 同样拒掉。

**假 provider**：`ScriptedReply(tool_calls=(...))` 能演"先调工具、再给最终回答"，
并刻意把参数切成两段吐出来，用来验证聚合逻辑。

### 六、`llm` 不负责重试、不负责计量

- 重试：将来做 `llm-retry` 能力插件（挂在失败步骤的扩展点上）；
- 计量：`token-usage` 能力插件订阅事件（M3）。
这与 dsh 的分工一致，也符合 0041 的判据（能力插件装卸只多/少一件事）。

## 备选与排除理由

**A. 一个插件一个模型**：换模型 = 改装配单 + 重装；会话无法各用各的。否决。
**B. 模型写进 `llm` 插件的构造参数**：同 A，而且"默认模型"会被误当成"唯一模型"。
否决。
**C. 每个会话 fork 一套 llm 服务**（cordis 的 isolate）：P4 是单租户单命名空间
（0042），为此引入作用域隔离不划算。否决。

## 后果

- `agent-loop` 只依赖 `llm` 服务与自己的 `AgentOptions`，与"循环是插件"（差距 1）
  正交：换循环、换模型互不影响；
- 适配器卸载时路由随之消失：正在跑的请求会 `NO_PROVIDER` 失败（fail loud），
  不会静默用别的模型；
- M2 的第一步只做服务 + 假适配器；真实 DeepSeek 适配器等基础 turn 跑通后再加。

## 相关

- [0041](0041-p4-substantiation.md)（三类插件）、[0042](0042-p4-base-boundary.md)
  （基座边界与角色归属）、[0043](0043-p4-rewrite-from-scratch.md)（从零重写）；
- dsh 对照：`packages/llm/llm`（路由器，无 provider 代码）、
  `packages/llm/llm-deepseek` / `llm-pi-ai`（适配器）、`llm-retry`（重试）、
  `token-meter`（计量）；`packages/core/agent/src/runtime-types.ts` 的 `AgentOptions`。
