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
from kernel import Context
from plugins.events import LLM_REQUEST, LLM_RESPONSE
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
            """包装内层 provider：请求瀑布 + 响应广播（循环完全无感）。"""

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
