"""harness —— mini agent harness 的 M1–M3 基线库（P2 阶段目录里冻结的那一份）。

从 P1 的四份自包含副本收敛而来（消除"每个练习一份同名包"的重复）：

    harness.llm       LLM 接缝：协议 + 中立词汇 + FakeLLM + DeepSeekProvider
    harness.agent     主循环：turn/step 词汇 + 调用轨迹 + 取消
    harness.tools     工具系统：注册表（定义/实现分离）+ 审批 + pre/execute/post 管线
    harness.session   会话日志：append-only 事件 + 投影 + JSONL 落盘 + resume
    harness.mini      装配类 MiniHarness（把上面四者接成一条会话）
    harness.env       环境与 provider 构造（唯一的"选供应商"落点）
    harness.cli       命令行入口（python -m harness）
    harness.webui     本地可视化页面（stdlib http.server + NDJSON 事件流）

P2 组织约定：**本包是各阶段共享的基线，阶段新增的机制不放这里**。每个阶段目录的结构是

    _0N_<主题>/
    ├── harness/          ← 本文件所在的基线库（各阶段自带的副本）
    ├── <新模块>/         ← 该阶段新增的机制，作为顶层模块与 harness/ 平级
    ├── tests/  demo.py  conftest.py  run.bat/run.sh

`harness/config`、`harness/providers`、`harness/server` 是为后续阶段预留的占位子包，
但目前按约定，M4 起的新增机制将落在阶段主目录的顶层（见 `_01_Prompt_Sections/prompt/`）。
"""

__version__ = "0.2.0"
