# _01_LLM_Calling —— 最小化模型调用与提示词拼装

P0 第一个练习：一个可直接运行的单文件脚本，完成"装配提示词 → 调用模型 → 流式拿到回答"。

## 学习目标

1. 跑通一次真实的 LLM API 调用（DeepSeek，OpenAI 兼容协议），理解 messages 结构。
2. 理解**提示词是装配出来的**：若干小节各自独立、支持 `{{变量}}` 插值、按注册顺序拼接。
   这是 harness 里 system prompt 装配（学习计划 M4）的种子。
3. 两个工程习惯：**fail loud**（变量没解析出来就直接报错）、**先打印装配结果再调用**（可离线检查）。

## 文件

| 文件 | 说明 |
|---|---|
| `llm_calling.py` | 主脚本：单文件平铺，按注释分 6 节，从上到下执行 |
| `run.bat` / `run.sh` | 一键运行启动器（双击 `run.bat` 即可用） |
| `test_llm_calling.py` | 验收测试（黑盒跑脚本，不联网、不需要 key） |

## 运行

```bash
cd P0_Coding/_01_LLM_Calling

# 一键运行（Git Bash；cmd 里直接用 run.bat，PowerShell 用 .\run.bat）
./run.bat                        # 不带参数：默认问题
./run.bat "什么是事件溯源？"      # 指定问题
bash run.sh "什么是事件溯源？"    # 或者用 shell 版

# 手动运行（等价）
../../.venv/Scripts/python.exe llm_calling.py
../../.venv/Scripts/python.exe llm_calling.py "什么是事件溯源？"

# 验收测试
../../.venv/Scripts/python.exe -m pytest -q
```

## 配置 API key

放在项目根目录 `AgentDevLearn/.env`（已配置好，且已加入 `.gitignore` 不入库）：

```
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat                  # 可选，默认 deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com    # 可选
```

脚本只在本地读取，不会把 key 打印到输出里。缺 key 时会在打印装配结果后明确报错退出。

## 验收标准

- [x] 运行脚本打印完整 system 提示词，无未解析的 `{{变量}}`（覆盖测试）
- [x] 命令行问题能传入并出现在输出中（覆盖测试）
- [x] `pytest -q` 全部通过（2 个用例）
- [x] 流式收到模型回答（2026-09-30 用真实 key 验证）

## 脚本的 6 节结构（对应 harness 概念）

```
1) sections     提示词小节       ↔ dsh 的 ctx.systemPrompt.section() 注册
2) variables    运行时变量       ↔ 运行时上下文投影（日期/平台/目录装配时才取值）
3) 装配         {{变量}} 插值    ↔ system prompt 装配；未解析变量 fail loud（铁律 #8）
4) .env 读取    API 配置
5) messages     system + user   ↔ system 装装配产物，user 只放本次问题
6) 调用         流式打印         ↔ 为 M1 的 llm/stream 事件铺路
```

## 下一步（M1 预告）

现在模型只会"说"不会"做"。下一步把这段调用封装成 `LLMProvider` 协议 + 离线可断言的 `FakeLLM`，
然后写 agent 主循环：模型 → 工具调用 → 工具结果 → 模型 ……直到得出最终回答。
