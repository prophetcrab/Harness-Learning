"""LLM 接缝的 Service Definition —— 协议与请求/响应类型。

这是循环唯一允许依赖的模型接口：`agent_loop` 相关代码里不出现 `openai`、
不出现 `chat.completions`。换供应商 = 新增一个满足本协议的类。

对应 dsh：`packages/llm/llm/src/index.ts` 的抽象类 `LlmAdapter`
（`stream(GenerateOptions) -> AsyncIterable<StreamChunk>`）。
差异说明：本练习先做"请求 → 完整响应"的非流式版 `complete()`；
流式（增量帧、聚合、取消）是后续主题，取舍理由见
`docs/decisions/0001-llm-provider-seam.md`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from llm_seam.vocabulary import Message, ToolSpec, Usage


@dataclass
class LLMRequest:
    """一轮模型调用的请求（中立格式）。

    messages 是"完整对话历史"的快照 —— 模型无状态，每次都要全量重发。
    """

    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    model: str | None = None  # None 表示使用 provider 的默认模型


@dataclass
class LLMResponse:
    """一轮模型调用的响应（中立格式）。"""

    message: Message             # 恒为 assistant 消息
    finish_reason: str = "stop"  # "stop" | "tool_calls" | ... 原样保留，便于调试
    usage: Usage | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """模型供应商协议。任何提供 complete() 的对象都满足它。

    用 Protocol 而不是抽象基类（ABC）的原因：
    - 实现者无需显式继承，第三方 provider 可以零依赖接入；
    - `runtime_checkable` 让 isinstance 检查（测试用）成为可能；
    - 类型检查器按"结构"判断，不按继承链判断 —— 这正是接缝要的松散耦合。
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        """执行一次完整调用：阻塞直到拿到完整响应。

        实现方职责：把中立请求翻译成自家方言、发起真实（或模拟）调用、
        再把结果翻译回 LLMResponse。失败处理策略（重试等）暂未定义，
        属于后续主题（dsh 里对应 llm-retry 插件）。
        """
        ...
