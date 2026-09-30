# _02_Tool_Calling —— 最小化工具调用与结果展示

P0 第二个练习：让模型从"只会说"变成"会先查再答"。
用一个无需 API key 的网络搜索工具（Bing），演示完整的工具调用链路。

## 学习目标

1. 理解**工具调用的闭环**：模型并不执行任何东西，它只是"请求"调用，真正执行的是你的代码。
2. 掌握四个关键对象：**工具说明书（JSON Schema）→ tool_calls 请求 → Python 执行 → tool 消息回填**。
3. 体会两条设计原则：
   - 工具出错要转成**结构化错误回给模型**（铁律 #7），而不是让整个脚本崩掉；
   - 循环必须有**轮数上限**，防止模型反复调用工具停不下来。

## 文件

| 文件 | 说明 |
|---|---|
| `tool_calling.py` | 主脚本：工具定义 + Bing 搜索实现 + 主循环，单文件平铺 |
| `inspect_model_calls.py` | 观察脚本：逐次打印每次模型调用的请求摘要与原始返回 |
| `run.bat` / `run.sh` | 一键运行启动器（双击 `run.bat` 即跑完整链路） |
| `test_tool_calling.py` | 验收测试（离线用例 + 联网用例，断网自动跳过） |

## 运行

```bash
cd P0_Coding/_02_Tool_Calling

# 一键运行（Git Bash；cmd 里直接用 run.bat，PowerShell 用 .\run.bat）
./run.bat "今天上海天气怎么样？"          # 完整链路：模型自主搜索
./run.bat --search-only "python 教程"    # 只测搜索工具，不调模型
./run.bat inspect "今天上海天气怎么样？"  # 观察每次模型调用的请求与返回
bash run.sh "今天上海天气怎么样？"        # 或者用 shell 版

# 手动运行（等价）
../../.venv/Scripts/python.exe tool_calling.py "今天上海天气怎么样？"
../../.venv/Scripts/python.exe tool_calling.py --search-only "python 教程"
../../.venv/Scripts/python.exe inspect_model_calls.py "今天上海天气怎么样？"

# 验收测试
../../.venv/Scripts/python.exe -m pytest -q
```

## 一次运行的完整流程

```
本轮提供给模型的工具：[web_search] 网络搜索
问题：今天上海天气怎么样？

--- 第 1 轮：请求模型 ---
[工具调用] web_search({"query": "上海今天天气"})     ← 模型请求调用（它自己编的参数）
[工具结果] 关键词「上海今天天气」共 5 条：...          ← 你的代码真正执行了搜索
（已把工具结果回填给模型，进入下一轮）
--- 第 2 轮：请求模型 ---
=== 最终回答 ===                                     ← 模型基于搜索结果作答
...
```

## 观察每一次模型调用（inspect_model_calls.py）

`tool_calling.py` 只打印"发生了什么"；`inspect_model_calls.py` 把每一次模型调用的
**请求（发过去哪些消息）** 和 **原始返回（finish_reason / content / tool_calls / token）** 完整摊开，
用来观察两个事实：

1. **模型是无状态的**——每次调用都要重发完整历史，tokens 逐轮增长：

```
════════════ 各次调用的 token 消耗 ════════════
  轮次    prompt(输入)    completion(输出)
    1             397               54
    2             900              125
    3            1451              177
prompt tokens 逐轮增长 —— 因为每次调用都要把完整对话历史重新发过去（模型无状态）。
```

2. **`tool_calls` 只是"申请"**——返回里 `finish_reason='tool_calls'`，`arguments` 是 JSON 字符串
   （不是字典）；工具在下一行 `[执行]` 处才真正运行。

```
[返回] finish_reason   = 'tool_calls'   ← 'tool_calls'=要调工具 / 'stop'=最终回答
       message.content = ''
       message.tool_calls：1 个申请（注意：这只是申请，还没执行）
         [0] id        = call_00_Bvt2kMpEf903bTpkQ6fe1439
              name      = web_search
              arguments = '{"query": "上海今天天气"}'
```

## 验收标准

- [x] `--search-only` 能独立完成搜索并格式化展示结果（已验证）
- [x] 完整链路中模型自主发起了 `web_search` 调用并基于结果作答（已验证：天气、DeepSeek 模型两个问题）
- [x] `pytest -q` 全部通过（2 个用例）
- [x] 没有 key 时明确报错并提示 `--search-only` 出路

## 四个关键对象（代码里的位置）

| 对象 | 是什么 | 代码位置 |
|---|---|---|
| 工具说明书 | 给模型看的 JSON Schema：名字、用途、参数 | 第 1 节 `tools_schema` |
| 工具实现 | 真正执行的 Python 函数 | 第 2 节 `web_search()` |
| 工具注册表 | 名字 → 函数的对照表 + 错误包装 | 第 3 节 `tool_functions` / `execute_tool()` |
| 主循环 | 请求模型 → 执行工具 → 回填结果 → 再请求 | 第 7 节 |

**协议要点**：assistant 消息（含 `tool_calls`）必须原样存入历史；`role="tool"` 的结果必须带 `tool_call_id` 与请求配对——模型靠这个对应关系知道"这条结果是哪次调用的"。

## 已知限制（故意的，后续练习解决）

- **只有一个工具**：注册表还是硬编码字典，没有防护管线（审批/超时）——对应 M2。
- **没有日志**：对话全在内存里，关掉就没了——对应 M3 会话日志。
- **搜索质量依赖 Bing 页面结构**，正则解析很脆弱；练习重点是链路，不是抓取。

## 下一步（M1/M2 预告）

把 `tool_functions` 字典升级成带 schema、超时和审批的注册表；给模型加上文件读写和 shell 工具；
再往前一步，把"模型 → 工具 → 模型"的循环抽成正式的 agent loop。
