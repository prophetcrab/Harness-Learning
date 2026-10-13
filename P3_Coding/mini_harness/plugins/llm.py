"""llm 插件 —— 提供 LLM 能力（`llm` 服务）。

一个薄封装：把任意的 `LLMProvider` 实现装进 ctx，并在**每次模型调用前后**
广播 `llm/request` / `llm/response` 事件（观测与计量钩子），调用前还经过一次
`llm/request` 瀑布（其它插件可以改写请求，例如换模型、加参数）。

provider 的具体选择由工厂函数给出——这正是"换供应商不碰上层"的落点：

    fake_llm_plugin(script)              离线剧本（测试/演示默认）
    deepseek_llm_plugin(project_root)    真实 API（从项目根 .env 读 key）
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


def fake_llm_plugin(script: list[Any] | None = None) -> LlmPlugin:
    """离线 provider 插件：剧本队列（缺省一条最简单的最终回答）。"""
    return LlmPlugin(FakeLLM(script if script is not None else [text_reply("（离线回答）")]))


def deepseek_llm_plugin(project_root: str | Path) -> LlmPlugin:
    """真实 provider 插件：从项目根 .env 读 key；缺失直接 fail loud。"""
    from components.env import build_deepseek_provider

    return LlmPlugin(build_deepseek_provider(Path(project_root)))


__all__ = ["LlmPlugin", "deepseek_llm_plugin", "fake_llm_plugin"]
