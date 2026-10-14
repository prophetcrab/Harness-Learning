"""FakeLLM —— 离线、可断言的模型供应商实现。

设计要点（为什么这样，而不是用 mock 库给 openai 打桩）：
1. FakeLLM 实现的是和真实 provider 相同的协议，被测代码感知不到区别；
2. 断言对象是"循环发给模型的请求"（请求记录）与"循环怎么处理回复"，
   而不是 SDK 内部的调用细节 —— openai 升级、换 SDK 都不会影响测试；
3. 剧本把"模型行为"变成可编程的输入，测试从碰运气变成确定性。

对应 dsh：`packages/test-support/llm-replay`（录制回放）以及各测试里的
脚本化适配器；本项目用最朴素的"剧本队列"实现同一思想。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from components.llm.provider import LLMRequest, LLMResponse
from components.llm.streaming import (
    ScriptedStream,
    StreamChunk,
    aggregate_chunks,
    chunks_from_response,
)
from components.llm.vocabulary import ToolCall, assistant


class FakeLLM:
    """按剧本回复的 provider；同时记录收到的每一次请求。"""

    def __init__(self, script: Iterable[LLMResponse]) -> None:
        # 剧本 = 队列：每次 complete() 弹出一条，弹出的顺序就是"模型"的回复顺序。
        self._script: list[LLMResponse] = list(script)
        # 请求记录：每次 complete() 都会把请求存一份，供测试断言。
        # （例如断言"第 1 次请求只有 2 条消息，第 2 次请求有 4 条"。）
        self.requests: list[LLMRequest] = []

    # ---- 协议实现 ----

    def complete(self, request: LLMRequest) -> LLMResponse:
        """按剧本回复一次；剧本用完还继续调用则直接报错。

        剧本条目可以是 LLMResponse（直接返回）或 ScriptedStream（先聚合再返回）。
        """
        self.requests.append(request)
        if not self._script:
            raise AssertionError(
                f"FakeLLM 剧本已用完：这是第 {len(self.requests)} 次调用，"
                f"但剧本只准备了 {len(self.requests) - 1} 条回复。"
                "说明被测代码调用模型的次数比预期多了一次。"
            )
        item = self._script.pop(0)
        if _is_scripted_stream(item):
            return aggregate_chunks(item.chunks)
        return item

    def stream(self, request: LLMRequest) -> Iterator[StreamChunk]:
        """流式回复：剧本条目按类型播放（ScriptedStream 原样、LLMResponse 播放）。

        与 complete() 共享剧本队列与请求记录——同一份剧本，两种模式都能跑；
        这是"流式与非流式语义一致"的测试基础。
        """
        self.requests.append(request)
        if not self._script:
            raise AssertionError(
                f"FakeLLM 剧本已用完：这是第 {len(self.requests)} 次调用，"
                f"但剧本只准备了 {len(self.requests) - 1} 条回复。"
                "说明被测代码调用模型的次数比预期多了一次。"
            )
        item = self._script.pop(0)
        if _is_scripted_stream(item):
            yield from item.chunks
        else:
            yield from chunks_from_response(item)

    # ---- 测试辅助 ----

    @property
    def request_count(self) -> int:
        """模型被调用的次数。"""
        return len(self.requests)

    @property
    def remaining(self) -> int:
        """剧本里还没用掉的回复条数。"""
        return len(self._script)

    def assert_all_consumed(self) -> None:
        """断言剧本正好用完（循环没有少调模型）。"""
        if self._script:
            raise AssertionError(
                f"剧本还剩 {self.remaining} 条没用：循环提前结束了，"
                "检查是不是预期之外地拿到了最终回答。"
            )


# ---------------------------------------------------------------------------
# 剧本积木：用一行代码拼出一条"模型回复"
# ---------------------------------------------------------------------------


def _is_scripted_stream(item: object) -> bool:
    """判断剧本条目是不是流式剧本。

    **刻意不用 isinstance**：本项目各目录带同名包副本，全量跑测试时 conftest
    会驱逐模块缓存，于是"实现里的 ScriptedStream 类"与"测试里那个"可能是两个
    不同的类对象，isinstance 会假阴性（实测踩到：聚合分支没进，把 ScriptedStream
    当 LLMResponse 用，报 has no attribute 'message'）。鸭子类型按结构判断，
    跨模块身份也稳。
    """
    return hasattr(item, "chunks") and not hasattr(item, "message")


def text_reply(text: str, finish_reason: str = "stop") -> LLMResponse:
    """一条纯文本回复（没有工具申请）—— 通常是对话的最后一轮。"""
    return LLMResponse(message=assistant(text), finish_reason=finish_reason)


def tool_call_reply(
    name: str,
    arguments: dict,
    call_id: str = "call_1",
    content: str = "",
) -> LLMResponse:
    """一条"申请调用一个工具"的回复。

    content 可以非空：真实模型经常一边说"我来算一下"一边发申请。
    同一轮里多个申请的 id 必须互不相同（协议要求逐个配对）。
    """
    call = ToolCall(id=call_id, name=name, arguments=dict(arguments))
    return LLMResponse(message=assistant(content, [call]), finish_reason="tool_calls")


def streamed_text(text: str, *, piece: int = 4) -> ScriptedStream:
    """一条"逐段到达"的文本回复（测试流式路径用）。

    `piece` 是每片字符数——默认 4，让镜头里能看到明显的逐字效果。
    """
    chunks = [StreamChunk(text=text[i : i + piece]) for i in range(0, len(text), piece)]
    chunks.append(StreamChunk(finish_reason="stop"))
    return ScriptedStream(tuple(chunks))


def streamed_tool_call(
    name: str, arguments: dict, *, call_id: str = "call_1", piece: int = 5
) -> ScriptedStream:
    """一条"工具调用分片到达"的流式回复（参数是 JSON 字符串被切成几片）。"""
    raw = json.dumps(arguments, ensure_ascii=False)
    chunks: list[StreamChunk] = [
        StreamChunk(tool_index=0, tool_id=call_id, tool_name=name)
    ]
    for i in range(0, len(raw), piece):
        chunks.append(StreamChunk(tool_index=0, tool_arguments=raw[i : i + piece]))
    chunks.append(StreamChunk(finish_reason="tool_calls"))
    return ScriptedStream(tuple(chunks))


def tool_calls_reply(calls: list[tuple[str, dict]], content: str = "") -> LLMResponse:
    """一条"同时申请多个工具"的回复（并行工具调用）。

    calls 是 (工具名, 参数字典) 列表，id 自动编号为 call_1、call_2……
    """
    tool_calls = [
        ToolCall(id=f"call_{index}", name=name, arguments=dict(arguments))
        for index, (name, arguments) in enumerate(calls, start=1)
    ]
    return LLMResponse(message=assistant(content, tool_calls), finish_reason="tool_calls")
