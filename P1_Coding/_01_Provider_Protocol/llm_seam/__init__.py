"""llm_seam —— 本练习的包：LLM 供应商接缝的最小实现。

文件分工（也是推荐的阅读顺序）：

1. vocabulary.py  共享词汇：Message / ToolCall / ToolSpec / Usage
2. provider.py    接缝定义：LLMProvider 协议 + LLMRequest / LLMResponse
3. fake.py        离线实现：FakeLLM（剧本 + 请求记录）
4. deepseek.py    真实实现：DeepSeekProvider（方言翻译边界）
5. loop.py        消费者：run_tool_loop 只依赖协议
6. tools.py       示例工具：安全计算器 + 工具箱

三角色对应（capability seam）：
- Service Definition（接口）：provider.py
- Service Provider（实现）：fake.py / deepseek.py
- Consumer（使用者）：loop.py；组装发生在 demo.py
"""

from llm_seam.fake import FakeLLM, text_reply, tool_call_reply, tool_calls_reply
from llm_seam.loop import LoopResult, run_tool_loop
from llm_seam.provider import LLMProvider, LLMRequest, LLMResponse
from llm_seam.tools import Tool, Toolbox, build_default_toolbox
from llm_seam.vocabulary import (
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
    # 消费
    "LoopResult",
    "run_tool_loop",
    # 工具
    "Tool",
    "Toolbox",
    "build_default_toolbox",
]
