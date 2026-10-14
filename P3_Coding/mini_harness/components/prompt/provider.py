"""RuntimePromptProvider —— 每 step 渲染系统提示词的最小 provider 包装。

"每 step 渲染"需要一个"每一步都会被经过"的挂钩点。循环里唯一这样的点是
`provider.complete()`（每个 step 恰好调一次）——而 M1 把 provider 做成了接缝：
任何满足 `LLMProvider` 协议的对象都能插进循环，包装器不必认识 openai / DeepSeek。

包装器在每个 complete() 里做三件事：
1. **重新渲染**：用"当下"的运行时上下文渲染系统提示词（时间会走）；
2. **变更即记录**：渲染结果与"上一次已生效"的文本不同时，通过 on_render 回调
   让它进日志（system/message 事件）——回调携带完整的**装配单**（PromptTrace），
   于是日志里留下的不只是文本，还有"它是怎么拼出来的"（`_03` 新增）。
   不变就不重复记——日志只留"变化"，每一步看到的文本仍可由日志重建。
3. **改写请求**：把发给真实 provider 的请求里的 system 消息替换为最新渲染结果。
   模型这一步看到的就是它——不需要改动循环（harness/ 冻结）。

注意：循环内存里的历史（AgentLoop._history）中 system 文本停在"开会话那一刻"，
它不逐 step 更新；每一步的真实文本以日志为准（铁律 #1：模型可见 ⟺ 已记录）。
"""

from __future__ import annotations

from collections.abc import Callable

from components.llm.provider import LLMProvider, LLMRequest, LLMResponse
from components.llm.vocabulary import Message, system
from components.prompt.renderer import PromptRenderer
from components.prompt.trace import PromptTrace

# 记录回调：把一次"变更后的渲染"（连同装配单）交给日志。
# 接线层接 session.append("system/message", {"content": trace.text, ...})。
RenderSink = Callable[[PromptTrace], None]


def with_system_prompt(messages: list[Message], text: str) -> list[Message]:
    """把消息列表里的 system 消息替换为 text（没有 system 消息则插到最前）。"""
    result = list(messages)
    for index, message in enumerate(result):
        if message.role == "system":
            result[index] = system(text)
            return result
    result.insert(0, system(text))
    return result


class RuntimePromptProvider:
    """包装任意 provider：每 step 用最新上下文渲染系统提示词。

    **可选流式也透传**（`_02`）：内层支持 stream() 时，本类才挂上 stream
    属性（实例级，见 `__init__` 末尾）；内层不支持就不挂——循环用
    `hasattr(provider, "stream")` 探测，探测结果必须真实，否则"只有 complete
    的 provider"会被误判为流式、一调用就炸。
    """

    def __init__(
        self,
        inner: LLMProvider,
        renderer: PromptRenderer,
        *,
        on_render: RenderSink | None = None,
        initial: str | None = None,
    ) -> None:
        self._inner = inner
        self._renderer = renderer
        self._on_render = on_render
        # 上一次"已生效"的渲染文本：新会话 = session/start 里的初始文本；
        # resume = 日志里的当前生效文本（避免重开后第一步就重复记录）。
        self._last = initial
        self._last_trace: PromptTrace | None = None
        if hasattr(inner, "stream"):
            # 仅当内层真的流式时暴露 stream（诚实代理：探测接口不能撒谎）。
            self.stream = self._stream  # type: ignore[method-assign]

    @property
    def inner(self) -> LLMProvider:
        """被包装的真实 provider（只读）。"""
        return self._inner

    @property
    def last_rendered(self) -> str | None:
        """最近一次渲染并生效的文本（调试/演示用）。"""
        return self._last

    @property
    def last_trace(self) -> PromptTrace | None:
        """最近一次渲染的装配单（无论是否发生记录）——`_03` 的追溯入口。"""
        return self._last_trace

    def complete(self, request: LLMRequest) -> LLMResponse:
        trace = self._renderer.render_traced()
        self._last_trace = trace
        if trace.text != self._last:
            self._last = trace.text
            if self._on_render is not None:
                self._on_render(trace)
        return self._inner.complete(self._with_prompt(request, trace))

    def _stream(self, request: LLMRequest):
        """流式透传（`_02`）：同样每步重渲染，然后转发内层增量。"""
        trace = self._renderer.render_traced()
        self._last_trace = trace
        if trace.text != self._last:
            self._last = trace.text
            if self._on_render is not None:
                self._on_render(trace)
        yield from self._inner.stream(self._with_prompt(request, trace))

    @staticmethod
    def _with_prompt(request: LLMRequest, trace: PromptTrace) -> LLMRequest:
        """把最新渲染的系统提示词替换进请求（其它字段原样透传）。"""
        return LLMRequest(
            messages=with_system_prompt(request.messages, trace.text),
            tools=list(request.tools),
            model=request.model,
        )


__all__ = ["RenderSink", "RuntimePromptProvider", "with_system_prompt"]
