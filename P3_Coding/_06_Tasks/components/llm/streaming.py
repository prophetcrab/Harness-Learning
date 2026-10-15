"""流式增量与聚合 —— "逐段到达的回复"如何拼成一条完整响应。

`_02` 引入的最小词汇：一次模型回复在**传输层**是若干 `StreamChunk`（文本片段、
工具调用的增量），在**语义层**仍是一条 `LLMResponse`。两者之间由 `aggregate_chunks`
连接——它是一个**纯函数**：同样的增量序列永远聚出同样的响应，所以

- provider 只管"把厂商的 SSE 帧翻译成中立增量"（方言边界不变）；
- 循环只管"消费增量 + 聚合"（它拿到的东西与 `complete()` 一模一样）；
- 测试可以离线驱动整条链路（构造增量序列即可，不需要网络）。

聚合规则：
    text        顺序拼接；
    tool_calls  按 `tool_index` 分组，各自的 id/name/参数片段分别累积；
                参数拼接完成后才 `json.loads`（增量里是 JSON 字符串的碎片）；
    finish_reason  取最后一个非空值（通常只在末帧出现）。

失败是 fail loud：参数拼完不是合法 JSON（或缺名字）时抛 ValueError——
宁可让这一步炸掉，也不把半个工具调用塞给工具层。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from components.llm.provider import LLMResponse
from components.llm.vocabulary import Message, ToolCall, Usage


@dataclass(frozen=True)
class StreamChunk:
    """一个流式增量（文本片段 或 工具调用的一段）。

    - 纯文本增量：只填 `text`；
    - 工具调用增量：填 `tool_index`（同一轮里第几个申请），`tool_id` / `tool_name`
      通常只在第一片里出现，`tool_arguments` 是 JSON 字符串的**片段**（可能是
      `'{"expr'`、`'ession": "1+1"}'` 两片）；
    - 末帧：只填 `finish_reason`（"stop" / "tool_calls"）。
    """

    text: str = ""
    tool_index: int | None = None
    tool_id: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None   # 通常只在末帧出现（`_03`：用量也走流式路径）

    @property
    def is_tool_delta(self) -> bool:
        """这是不是一段工具调用增量。"""
        return self.tool_index is not None


@dataclass(frozen=True)
class ScriptedStream:
    """剧本里的一条"流式回复"：显式给出增量序列（测试用）。

    它存在 `FakeLLM` 的剧本队列里；`complete()` 遇到它会先聚合，
    `stream()` 遇到它会原样播放——同一份剧本两种模式都能跑。
    """

    chunks: tuple[StreamChunk, ...] = field(default_factory=tuple)


def aggregate_chunks(chunks: Iterable[StreamChunk]) -> LLMResponse:
    """把增量序列聚合成一条完整响应（纯函数，流式语义的唯一落点）。"""
    texts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    order: list[int] = []
    finish_reason = "stop"
    usage: Usage | None = None

    for chunk in chunks:
        if chunk.text:
            texts.append(chunk.text)
        if chunk.tool_index is not None:
            index = chunk.tool_index
            if index not in calls:
                calls[index] = {"id": "", "name": "", "arguments": ""}
                order.append(index)
            slot = calls[index]
            if chunk.tool_id:
                slot["id"] = chunk.tool_id
            if chunk.tool_name:
                slot["name"] = chunk.tool_name
            slot["arguments"] += chunk.tool_arguments
        if chunk.finish_reason:
            finish_reason = chunk.finish_reason
        if chunk.usage is not None:
            usage = chunk.usage

    tool_calls: list[ToolCall] = []
    for index in order:
        slot = calls[index]
        raw = slot["arguments"] or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"工具调用参数不是合法 JSON（工具 {slot['name'] or '?' }）：{raw!r}（{exc}）"
            ) from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"工具调用参数必须是 JSON 对象：{raw!r}")
        if not slot["name"]:
            raise ValueError("工具调用增量缺少工具名（name 只在首片出现）")
        tool_calls.append(
            ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=arguments)
        )

    return LLMResponse(
        message=Message(role="assistant", content="".join(texts), tool_calls=tool_calls),
        finish_reason=finish_reason,
        usage=usage,
    )


def chunks_from_response(response: LLMResponse) -> list[StreamChunk]:
    """把一条**一次性**响应"播放"成增量序列（聚合回去与原件等价）。

    用途：provider 不支持流式、但消费方按流式接口驱动时（统一形状）；
    以及测试里把 `text_reply` 这类剧本搬上流式路径。
    """
    chunks: list[StreamChunk] = []
    if response.message.content:
        chunks.append(StreamChunk(text=response.message.content))
    for index, call in enumerate(response.message.tool_calls):
        chunks.append(
            StreamChunk(
                tool_index=index,
                tool_id=call.id,
                tool_name=call.name,
                tool_arguments=json.dumps(call.arguments, ensure_ascii=False),
            )
        )
    chunks.append(StreamChunk(finish_reason=response.finish_reason, usage=response.usage))
    return chunks


def stream_response(response: LLMResponse) -> Iterator[StreamChunk]:
    """`chunks_from_response` 的迭代器版本（方便 `yield from`）。"""
    yield from chunks_from_response(response)


__all__ = [
    "ScriptedStream",
    "StreamChunk",
    "aggregate_chunks",
    "chunks_from_response",
    "stream_response",
]
