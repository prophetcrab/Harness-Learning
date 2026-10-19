"""llm-retry —— 重试策略（能力子插件）：挂在 `llm/call` 的 waterfall 上。

它是"策略插座"的真实使用者（0042 要闭的第三处差距之一）：llm 主干只负责
"发事件"，重试作为插件包住调用——装卸它，产品形态不变，只是少/多一项策略。

**默认只重试可恢复的错误**：

    LLM_CONNECTION / LLM_TIMEOUT / LLM_RATE_LIMIT / LLM_SERVER_ERROR
    （以及默认视为可恢复的 OSError / TimeoutError）

**绝不重试**：凭据无效、请求非法、没有适配器/模型——重试只会再失败一次。

流**保持流式**：重试只发生在"还没吐出任何块"的时候。首块一旦交给调用方，
就不再重试——否则重试会把前面已经输出的内容再吐一遍（重复文本）。代价是
"首块之后才断"的情况不重试（那属于半截回答，完整重试要看调用方策略）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from core import Context
from plugins.base.llm import LLM_CALL_EVENT, Chunk

#: 默认可重试的错误 code。
DEFAULT_RETRY_ON: tuple[str, ...] = (
    "LLM_CONNECTION",
    "LLM_TIMEOUT",
    "LLM_RATE_LIMIT",
    "LLM_SERVER_ERROR",
)

#: 永不重试的错误 code（即使配置把它们列进 retry_on）。
NEVER_RETRY_ON: tuple[str, ...] = (
    "LLM_AUTH",
    "INVALID_REQUEST",
    "NO_PROVIDER",
    "NO_MODEL",
)


class RetryPlugin:
    """把 `llm/call` 包成"最多 N 次、可选退避"的调用。"""

    name = "llm-retry"

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        backoff_seconds: float = 0.0,
        retry_on: Sequence[str] | None = None,
        retry_unknown: bool = True,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts 至少为 1，收到：{max_attempts}")
        self._max_attempts = int(max_attempts)
        self._backoff = max(0.0, float(backoff_seconds))
        self._retry_on = (
            frozenset(retry_on) if retry_on is not None else frozenset(DEFAULT_RETRY_ON)
        )
        self._retry_unknown = retry_unknown
        self._sleep = sleep or time.sleep

    def setup(self, ctx: Context) -> None:
        # 订阅随插件 effect 回卷：卸载 retry 插件，策略立刻消失。
        ctx.on(LLM_CALL_EVENT, self._wrap)

    # ------------------------------------------------------------------
    # waterfall handler
    # ------------------------------------------------------------------

    def _wrap(
        self,
        call: Callable[[Any], Iterator[Chunk]],
        **context: Any,
    ) -> Callable[..., Iterator[Chunk]]:
        del context  # 策略只看请求本身（req），不需要额外上下文

        def retrying(request: Any) -> Iterator[Chunk]:
            return self._run(call, request)

        return retrying

    def _run(
        self, call: Callable[[Any], Iterator[Chunk]], request: Any
    ) -> Iterator[Chunk]:
        last_error: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            emitted = False  # 已经吐给调用方内容了吗（吐了就不能重试）
            retry_reason: object = None
            try:
                for chunk in call(request):
                    if (
                        not emitted
                        and getattr(chunk, "type", None) == "finish"
                        and getattr(chunk, "reason", None) == "error"
                        and attempt < self._max_attempts
                        and self._retryable_code(getattr(chunk, "error_code", None))
                    ):
                        # 首块就是错误结束：还没输出任何东西，可以重试。
                        retry_reason = (
                            getattr(chunk, "error", None)
                            or getattr(chunk, "error_code", None)
                            or "error"
                        )
                        break
                    emitted = True
                    yield chunk
                if emitted:
                    return
                if retry_reason is None:
                    # 一个块都没有：空响应，保守地按可重试处理。
                    retry_reason = "provider 没有返回任何块"
            except Exception as exc:  # noqa: BLE001 —— 分类后决定是否重试
                if emitted:
                    # 已经吐出过内容：不能重试（会把已输出的部分再吐一遍）。
                    raise
                if attempt >= self._max_attempts or not self._retryable(exc):
                    raise
                retry_reason = exc
                last_error = exc

            if attempt >= self._max_attempts:
                # 用尽重试：抛出**最后一次的原始异常**（保留 code）。
                if last_error is not None:
                    raise last_error
                raise RuntimeError(
                    f"重试 {self._max_attempts} 次仍失败：{retry_reason}"
                )
            self._wait(attempt, retry_reason)

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def _retryable(self, exc: BaseException) -> bool:
        code = getattr(exc, "code", None)
        if code is not None:
            return self._retryable_code(str(code))
        # 非 LLM 层的异常：连接/超时类默认重试（可关掉）。
        return self._retry_unknown and isinstance(exc, (OSError, TimeoutError))

    def _retryable_code(self, code: str | None) -> bool:
        if code is None:
            return False
        if code in NEVER_RETRY_ON:
            return False
        if code in self._retry_on:
            return True
        # 所有 LLMError 都有 code；没列进白名单的错误不重试。
        return (
            isinstance(code, str)
            and code.startswith("LLM_")
            and self._retry_unknown
            and code not in NEVER_RETRY_ON
        )

    def _wait(self, attempt: int, reason: object) -> None:
        if self._backoff > 0:
            self._sleep(self._backoff * attempt)


def llm_retry_plugin(**config: Any) -> RetryPlugin:
    """工厂：装配单的 `config` 直接喂给构造参数。"""
    return RetryPlugin(**config)


__all__ = ["DEFAULT_RETRY_ON", "NEVER_RETRY_ON", "RetryPlugin", "llm_retry_plugin"]
