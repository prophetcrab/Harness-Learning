"""llm 插件 —— 提供 LLM 能力（`llm` 服务）。

一个薄封装：把任意的 `LLMProvider` 实现装进 ctx，并在**每次模型调用前后**
广播 `llm/request` / `llm/response` 事件（观测与计量钩子），调用前还经过一次
`llm/request` 瀑布（其它插件可以改写请求，例如换模型、加参数）。

provider 的具体选择由工厂函数给出——这正是"换供应商不碰上层"的落点：

    fake_llm_plugin(script)              离线剧本（**严格**：用完还调就报错；测试用）
    offline_llm_plugin(reply)            离线固定回复（**无限**：交互演示用）
    deepseek_llm_plugin(project_root)    真实 API（从项目根 .env 读 key）

两类离线 provider 的分工是刻意的：剧本队列的纪律（"多调一次模型就报错"）是
测试的武器，但对"人对着界面问十几轮"的场景是折磨；交互演示要的是"问不完"。
选哪个由装配方（`boot_offline(script=…)`）决定。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from components.llm import FakeLLM, LLMProvider, LLMRequest, LLMResponse
from components.llm.fake import text_reply
from components.llm.streaming import StreamChunk
from components.llm.vocabulary import Message
from kernel import Context
from plugins.events import LLM_REQUEST, LLM_RESPONSE, LLM_STREAM
from plugins.services import LLM_SERVICE

#: 交互用的离线默认回复（能看出"这是离线模拟"，且提示怎么切真实模式）。
OFFLINE_DEMO_REPLY = "（离线演示回复：当前是 FakeLLM，回答固定；用 --realtime 接真实模型）"


class LlmPlugin:
    """把一个 LLMProvider 装成 `llm` 服务（带事件广播与请求瀑布）。"""

    name = "llm"

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def setup(self, ctx: Context) -> None:
        inner = self._provider

        class _Instrumented:
            """包装内层 provider：请求瀑布 + 响应广播（循环完全无感）。

            可选流式的**诚实代理**：内层没有 stream() 时，本包装器也**不暴露**
            stream 属性——这样循环的 `hasattr(provider, "stream")` 探测得到
            真实答案，自然退回一次性 complete()。曾经写成"总是暴露、调用时才
            报错"，结果循环探测到就调用，一个只有 complete 的 provider 直接炸——
            探测接口不能撒谎。
            """

            def complete(self, request: LLMRequest) -> LLMResponse:
                request = ctx.waterfall(LLM_REQUEST, request)
                start = time.perf_counter()
                response = inner.complete(request)
                duration_ms = (time.perf_counter() - start) * 1000.0
                ctx.emit(
                    LLM_RESPONSE,
                    request=request,
                    response=response,
                    duration_ms=duration_ms,
                )
                return response

        if hasattr(inner, "stream"):
            def _stream(self, request: LLMRequest):
                """流式转发：请求过同一道瀑布、每个增量广播 llm/stream。"""
                request = ctx.waterfall(LLM_REQUEST, request)
                start = time.perf_counter()
                text_chars = 0
                usage = None
                for chunk in inner.stream(request):
                    if chunk.text:
                        text_chars += len(chunk.text)
                    if chunk.usage is not None:
                        # 用量通常只在末帧出现（`_03`）：留着，随 llm/response 广播
                        usage = chunk.usage
                    ctx.emit(LLM_STREAM, chunk=chunk)
                    yield chunk
                ctx.emit(
                    LLM_RESPONSE,
                    request=request,
                    # 流式路径拿不到"完整的 LLMResponse"（聚合发生在循环里），
                    # 但用量必须能传出去——订阅者（用量账本）只看 usage，
                    # 所以这里构造一个只带用量的轻量响应（其余字段为默认值）。
                    response=LLMResponse(
                        message=Message(role="assistant"), usage=usage
                    ) if usage is not None else None,
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                    streamed_chars=text_chars,
                )

            _Instrumented.stream = _stream   # 仅当内层支持时才挂上

        ctx.provide(LLM_SERVICE, _Instrumented())


class RepeatingLLM:
    """离线固定回复的 provider：**永远答得上**（交互演示专用）。

    与 FakeLLM 剧本队列相对：那个用完即报错（测试要抓"多调了一次"），
    这个不设上限（人对着界面聊多少轮都行）。它是"演示用的假模型"，
    不是"可断言的假模型"——两侧各有各的用处。
    """

    def __init__(self, reply: str = OFFLINE_DEMO_REPLY) -> None:
        self._reply = reply
        self.calls = 0  # 观测用：被调用过的次数

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return text_reply(self._reply)

    def stream(self, request: LLMRequest):
        """逐字播放固定回复（`_02`）：交互演示能看到确凿的流式效果。

        切片大小 2 个字符——足够看出"逐段到达"，又不至于把事件总线刷爆。
        """
        self.calls += 1
        text = self._reply
        for index in range(0, len(text), 2):
            yield StreamChunk(text=text[index : index + 2])
        yield StreamChunk(finish_reason="stop")


def fake_llm_plugin(script: list[Any] | None = None) -> LlmPlugin:
    """离线 provider 插件：剧本队列（缺省一条最简单的最终回答）。用完即报错。"""
    return LlmPlugin(FakeLLM(script if script is not None else [text_reply("（离线回答）")]))


def offline_llm_plugin(reply: str = OFFLINE_DEMO_REPLY) -> LlmPlugin:
    """离线 provider 插件：固定回复、不限轮数（交互界面/长演示用）。"""
    return LlmPlugin(RepeatingLLM(reply))


def deepseek_llm_plugin(project_root: str | Path) -> LlmPlugin:
    """真实 provider 插件：从项目根 .env 读 key；缺失直接 fail loud。"""
    from components.env import build_deepseek_provider

    return LlmPlugin(build_deepseek_provider(Path(project_root)))


__all__ = [
    "OFFLINE_DEMO_REPLY",
    "LlmPlugin",
    "RepeatingLLM",
    "deepseek_llm_plugin",
    "fake_llm_plugin",
    "offline_llm_plugin",
]
