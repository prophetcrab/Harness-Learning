# _02_Agent_Loop —— 正式的 Agent 主循环（turn/step + 轨迹 + 取消）

P1 第二个练习，对应学习计划 **M1** 的主循环部分：把 _01 里的 `run_tool_loop`
函数升级成可复用的 `AgentLoop` 类，并首次引入 **turn / step** 两层词汇。

## 学习目标

1. 看清 **turn 与 step 的区别**：一次 `run()` 是一个 turn（一次用户输入排空）；
   turn 内部每轮"模型请求 + 工具执行"往返是一个 step。一个 turn 可以有多个 step。
2. 掌握**调用轨迹**：循环的产出从"扁平的 status + messages"升级成可事后审查的
   `TurnResult → Step → ToolResult` 三层对象——请求快照、模型回复、工具结果都留痕。
3. 理解**取消**：循环在 step 边界接受外部打断（`cancel()` / `should_stop` 回调），
   结构化返回 `cancelled`，而不是抛异常或死循环。
4. 体会**历史由循环持有**：system 提示只注入一次，多个 turn 的历史跨 turn 累积。

## 文件（推荐阅读顺序）

| 顺序 | 文件 | 内容 | 对应 dsh |
|---|---|---|---|
| 1 | `agent_loop/trace.py` | 轨迹词汇：TurnResult / Step / ToolResult | `core/agent-loop` 的 step/turn 类型 |
| 2 | `agent_loop/loop.py` | `AgentLoop` 类：主循环 + 事件回调 + 取消 | `core/agent-loop/src/agent.ts` |
| 3 | `agent_loop/__init__.py` | 包出口 | — |
| 4 | `llm_seam/` | 自包含的 LLM 接缝副本（协议 + 词汇 + Fake/DeepSeek + 工具箱） | `packages/llm/llm/` |
| — | `demo.py` | 入口：`--fake` 离线 / 默认真实 API | — |
| — | `test_agent_loop.py` | 13 个验收测试（全离线） | — |
| — | `conftest.py` | pytest 约定说明（无额外引导） | — |

> `llm_seam/` 是 P1 `_01` 同名包的副本，复制进 `_02` 使本练习**完整独立**（不依赖
> sibling 目录）。相对 `_01` 原版删除了 `loop.py`——`run_tool_loop` 的消费角色已由
> `AgentLoop` 接替；其余文件原样保留。

## 运行

```bash
cd P1_Coding/_02_Agent_Loop

# 离线剧本（不需要 key，秒级）
./run.bat --fake
bash run.sh --fake

# 真实 API（需要项目根 .env 里的 key）
./run.bat "帮我算 987654*321 再除以 7"

# 测试（全离线）
python -m pytest -q        # 13 个用例
```

## 验收标准

- [x] 两步收尾：工具申请 → 结果 → 最终回答（FakeLLM 驱动，全程离线）
- [x] 轨迹是一等对象：每个 Step 的请求快照 / 回复 / 工具结果都可断言，且请求快照
      反映"当时"的历史（不被后续 append 污染）
- [x] turn/step 词汇：一个 turn 内多个 step；多 turn 历史累积、system 只注入一次
- [x] 触顶停止：超出 step 预算返回结构化 `max_steps`，不死循环、不抛异常
- [x] 工具报错恢复：错误回填给模型并收尾；执行器直接崩溃也被包装、不炸循环
- [x] 取消：`cancel()` 与 `should_stop` 回调都在 step 边界生效，返回 `cancelled`
- [x] 事件流按序触发：turn_start → (step_request → step_response → tool_result)* → turn_end
- [x] fail loud：`max_steps < 1` 直接报错
- [x] `pytest -q` 全部通过（13 个用例，0.03 秒）

## 真实运行示例（FakeLLM）

```
── turn 1 开始：帮我算 987654 * 321，再把结果除以 7 等于多少？
   [step 1] 请求模型：2 条消息 + 1 个工具
                ← 回复：申请调用 ['calculate']（还没执行）
                → 执行 calculate({"expression": "987654*321"}) 成功：{"result": 317036934}
   [step 2] 请求模型：4 条消息 + 1 个工具
                ← 回复：申请调用 ['calculate']（还没执行）
                → 执行 calculate({"expression": "317036934/7"}) 成功：{"result": 45290990.57…}
   [step 3] 请求模型：6 条消息 + 1 个工具
                ← 回复：最终回答（finish_reason=stop）
   ── turn 1 结束：status=done，共 3 个 step
```

同一个 turn 里发生了 3 个 step——这就是 turn/step 两层词汇想表达的：
"用户一次提问" 与 "模型一次往返" 是两个层次。

## 三个值得记住的设计点

1. **`run()` 是一次 turn，`complete()` 是一次 step 的一半**。循环把步数预算花在
   step 上（`max_steps` 是"最多几个 step"，不是"最多几次 run"），而 turn 只是
   用户输入与历史累积的边界。
2. **轨迹里的 `request` 是快照**。模型无状态，每步都要全量重发历史；快照独立
   意味着"模型这一步看到了什么"事后可查、且不会被后续 append 倒灌（铁律 #1 的雏形）。
3. **取消只在检查点生效**。同步循环无法打断一次正在进行的 `complete()`，
   因此 `cancel()` / `should_stop` 在每次 step（模型调用）之前检查。工具执行
   内部的超时中断属于 `_03_Tool_Pipeline`。

## 已知限制（故意的，后续主题解决）

- 历史仍在内存里，进程退出即忘（`_04_Session_Log` 做 JSONL 持久化 + resume）；
- `complete()` 仍非流式，无 token 级逐字输出（与 _01 相同的债，见 decisions/0001）；
- 取消是"粘性"的，一旦 cancel 后续所有 turn 都停；没有 turn 级取消重置；
- 工具的审批 / 超时 / pre-post 管线还没做（`_03_Tool_Pipeline`）。
