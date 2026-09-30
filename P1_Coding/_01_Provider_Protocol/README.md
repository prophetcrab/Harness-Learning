# _01_Provider_Protocol —— LLM 接缝：协议 + 两种实现

P1 第一个练习，对应学习计划 **M1** 的第一步：把 P0 里散落的模型调用升级成
**接缝（capability seam）**——定义、实现、消费者三个角色分离，换供应商只改一行组装代码。

## 学习目标

1. 看清"三角色"如何落地：**Service Definition**（`LLMProvider` 协议）/
   **Service Provider**（`FakeLLM`、`DeepSeekProvider`）/ **Consumer**（`run_tool_loop`）。
2. 掌握**方言翻译边界**：厂商专属细节（JSON 字符串形式的 arguments、`tool_call_id` 配对、
   `choices[0]`、`finish_reason`）全部关在 `deepseek.py` 里，闭环代码一行都不碰。
3. 体验**离线可断言测试**：`FakeLLM` 用"剧本 + 请求记录"把模型行为变成可编程输入，
   12 个测试 0.04 秒跑完，不联网、不需要 key。

## 文件（推荐阅读顺序）

| 顺序 | 文件 | 内容 | 对应 dsh |
|---|---|---|---|
| 1 | `llm_seam/vocabulary.py` | 共享词汇：Message / ToolCall / ToolSpec / Usage | `llm/llm/src/message.ts` |
| 2 | `llm_seam/provider.py` | 协议定义：`LLMProvider` + LLMRequest/LLMResponse | `llm/llm/src/index.ts` 的 `LlmAdapter` |
| 3 | `llm_seam/fake.py` | `FakeLLM`：剧本队列 + 请求记录 + 剧本积木 | `test-support/llm-replay` |
| 4 | `llm_seam/deepseek.py` | `DeepSeekProvider`：方言翻译（纯函数，可离线测） | `llm/llm-deepseek/src/adapter.ts` |
| 5 | `llm_seam/loop.py` | `run_tool_loop`：闭环消费者，只 import 协议 | `core/agent-loop` 的最简前身 |
| 6 | `llm_seam/tools.py` | 安全计算器 + 极简工具箱 | `core/tools` 的最简前身 |
| — | `demo.py` | 入口：`--fake` 离线 / 默认真实 API | — |
| — | `test_provider_protocol.py` | 12 个验收测试（全离线） | — |

## 运行

```bash
cd P1_Coding/_01_Provider_Protocol

# 离线剧本（不需要 key，秒级）
./run.bat --fake
bash run.sh --fake

# 真实 API（需要项目根 .env 里的 key）
./run.bat "帮我算 987654 * 321，再除以 7"

# 测试（全离线）
../../.venv/Scripts/python.exe -m pytest -q
```

## 验收标准

- [x] 两个 provider 都满足 `LLMProvider` 协议（`isinstance` 结构检查通过）
- [x] 翻译层纯函数验证：四种角色的 message ⇄ 线上格式双向正确（含 arguments 字符串/字典转换）
- [x] 闭环：单工具调用、并行双工具调用、工具报错恢复、触顶停止、请求快照不被后续历史污染
- [x] 剧本纪律：多用一次（剧本用完）和少用一次（有剩余）都会明确报错
- [x] `--fake` 与真实 API 两种模式实测通过
- [x] `pytest -q` 全部通过（12 个用例，0.04 秒）

## 真实运行示例（DeepSeekProvider）

```
[第 1 步] 请求模型：2 条消息 + 1 个工具
           ← 回复：申请调用 ['calculate']（还没执行）
           → 执行 calculate({"expression": "987654 * 321"}) 成功：{"result": 317036934}

[第 2 步] 请求模型：4 条消息 + 1 个工具
           ← 回复：申请调用 ['calculate']（还没执行）
           → 执行 calculate({"expression": "317036934 / 7"}) 成功：{"result": 45290990.57...}

[第 3 步] 请求模型：6 条消息 + 1 个工具
           ← 回复：最终回答（finish_reason=stop）
```

## 三个值得记住的设计点

1. **`arguments` 在词汇里是字典**。厂商返回的 JSON 字符串在 `deepseek.py` 里就被解析，
   解析失败也止步于 provider——循环和工具层永远拿到结构化的数据。
2. **`Protocol` 而非 ABC**。实现者零继承、第三方可零依赖接入；`runtime_checkable` 让
   `isinstance(provider, LLMProvider)` 可用，测试因此能证明"两个实现可互换"。
3. **`FakeLLM` 让"模型行为"可编程**。剧本是队列，用完即报错——这能精确抓住"循环多调了一次"
   或"提前退出"之类的 bug，而不是让测试碰运气。

## 已知限制（故意的，后续主题解决）

- 只有非流式 `complete()`；流式（增量帧、聚合、取消）在后续主题；
- 没有重试与错误分类（dsh 对应 `llm-retry`）；
- 工具只有"注册 + 查表 + 错误包装"，没有审批/超时/管线（`_03_Tool_Pipeline`）；
- 对话在内存里，退出即忘（`_04_Session_Log`）。
