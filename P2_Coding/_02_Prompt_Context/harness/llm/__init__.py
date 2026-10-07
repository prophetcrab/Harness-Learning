"""llm_seam —— LLM 供应商接缝的最小实现（_04 自包含的副本）。

来自 P1 _01 的同名包，复制进 _04 使本练习完整独立（不依赖 sibling 目录）。
相对 _01 原版删除了 loop.py（消费角色由 agent_loop.AgentLoop 接替），其余原样。

文件分工（推荐阅读顺序）：

1. vocabulary.py  共享词汇：Message / ToolCall / ToolSpec / Usage
2. provider.py    接缝定义：LLMProvider 协议 + LLMRequest / LLMResponse
3. fake.py        离线实现：FakeLLM（剧本 + 请求记录）
4. deepseek.py    真实实现：DeepSeekProvider（方言翻译边界）
5. tools.py       示例工具：安全计算器 + 工具箱

三角色对应（capability seam）：
- Service Definition（接口）：provider.py
- Service Provider（实现）：fake.py / deepseek.py
- Consumer（使用者）：agent_loop/loop.py；组装发生在 demo.py
"""

from harness.llm.fake import FakeLLM, text_reply, tool_call_reply, tool_calls_reply
from harness.llm.provider import LLMProvider, LLMRequest, LLMResponse
from harness.llm.tools import Tool, Toolbox, build_default_toolbox
from harness.llm.vocabulary import (
    Message,
    ToolCall,
    ToolSpec,
    Usage,
    assistant,
    system,
    tool_result,
    user,
)

__all__ = [
    # 词汇
    "Message",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "assistant",
    "system",
    "tool_result",
    "user",
    # 接缝定义
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    # 实现
    "FakeLLM",
    "text_reply",
    "tool_call_reply",
    "tool_calls_reply",
    # 工具
    "Tool",
    "Toolbox",
    "build_default_toolbox",
]
