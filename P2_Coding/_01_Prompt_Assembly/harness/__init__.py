"""harness —— mini agent harness（P2 起统一为单一包）。

从 P1 的四份自包含副本收敛而来（消除"每个练习一份同名包"的重复）：

    harness.llm       LLM 接缝：协议 + 中立词汇 + FakeLLM + DeepSeekProvider
    harness.agent     主循环：turn/step 词汇 + 调用轨迹 + 取消
    harness.tools     工具系统：注册表（定义/实现分离）+ 审批 + pre/execute/post 管线
    harness.session   会话日志：append-only 事件 + 投影 + JSONL 落盘 + resume
    harness.mini      装配类 MiniHarness（把上面四者接成一条会话）
    harness.env       环境与 provider 构造（唯一的"选供应商"落点）
    harness.cli       命令行入口（python -m harness）
    harness.webui     本地可视化页面（stdlib http.server + NDJSON 事件流）

P2（M4–M7）在此包上继续生长，占位子包：
    harness.prompt       M4 系统提示与上下文装配
    harness.providers    M5 能力接缝（FileSystem / SubprocessService）
    harness.config       M6 profile 式组装
    harness.server       M7 JSON-RPC 服务 + 事件流 follow
"""

__version__ = "0.2.0"
