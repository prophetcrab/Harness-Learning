"""传输层 —— LineTransport：换行 JSON-RPC 的收发循环（stdio 与测试共用一条路径）。

一行进、一行出：

    serve_forever()   从 reader 逐行读帧 → parse_line → service.dispatch
                      → 请求回 result/error 帧；通知不回（协议规定）
    读到 EOF          正常收尾（stdin 关闭 = 客户端断开；这就是"服务"的生命周期）

错误翻译（本层的全部职责就是把异常变成**稳定形状**的错误帧）：

    parse_line 抛 RpcError         → 按它自己的码回错误帧（id 尽力带回：能解析出 id 就带）
    service.dispatch 抛 RpcError   → 按它自己的码回错误帧（方法/参数问题由服务层定码）
    service.dispatch 抛其它异常     → INTERNAL_ERROR（-32603）+ 异常消息（不让服务进程崩）

**id 的尽力归属**：按 JSON-RPC 规范，解析错误无法知道 id 时应回 `id: null`；但
"帧能解析、只是字段不合法"（如 params 不是对象）时应尽量带回 id，客户端才能把
错误配对到它发出的请求。parse_line 出错后我们做一次**宽松提取**（能取到合法 id 就取）。

**通道纪律**：只写 reader/writer 给定的流；banner/日志这类人看的东西不归本层
（`serve.py` 写 stderr）。测试因此可以用 StringIO 驱动整条收发路径——与真实
stdio 完全同构（这正是 transport 独立成层的价值）。
"""

from __future__ import annotations

import json
from typing import Any, Protocol, TextIO

from server.protocol import (
    INTERNAL_ERROR,
    Request,
    RpcError,
    error_frame,
    parse_line,
    result_frame,
)


class Dispatcher(Protocol):
    """传输层对服务的唯一要求：dispatch(method, params) -> 可序列化结果。"""

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        ...


def _salvage_id(line: str) -> int | str | None:
    """从一行（可能不合法）的文本里尽力捞出合法的 id；捞不到返回 None。"""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
        return None
    return identifier


class LineTransport:
    """换行 JSON-RPC 传输：从 reader 收帧、把结果写回 writer。"""

    def __init__(self, reader: TextIO, writer: TextIO, service: Dispatcher) -> None:
        self._reader = reader
        self._writer = writer
        self._service = service

    # ------------------------------------------------------------------
    # 单行（可直接单测；serve_forever 只是它的循环壳）
    # ------------------------------------------------------------------

    def handle_line(self, line: str) -> str | None:
        """处理一行；请求返回响应帧文本，通知返回 None。异常不外抛。"""
        try:
            frame = parse_line(line)
        except RpcError as exc:
            return self._error_reply(_salvage_id(line), exc)

        if isinstance(frame, Request):
            return self._handle_request(frame)
        # 通知：分派但**不回响应**（协议规定）；分派异常同样静默丢弃。
        try:
            self._service.dispatch(frame.method, frame.params)
        except Exception:  # noqa: BLE001 —— 通知的错误无处可回，按协议丢弃
            pass
        return None

    def _handle_request(self, request: Request) -> str:
        try:
            result = self._service.dispatch(request.method, request.params)
        except RpcError as exc:
            return self._error_reply(request.id, exc)
        except Exception as exc:  # noqa: BLE001 —— 兜底：服务内部错误不让进程崩
            return self._error_reply(
                request.id, RpcError(INTERNAL_ERROR, f"服务内部错误：{exc}")
            )
        return result_frame(request.id, result)

    @staticmethod
    def _error_reply(request_id: int | str | None, exc: RpcError) -> str:
        return error_frame(request_id, exc.code, exc.message, exc.data)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def serve_forever(self) -> None:
        """逐行读帧直到 EOF；每行都立即处理并回写（行 = 帧，无缓冲粘包问题）。"""
        for raw_line in self._reader:
            if not raw_line.strip():
                # 空行照旧走 handle_line（回 PARSE_ERROR）——除非是文件末尾的收尾空行。
                if raw_line == "":
                    break
            reply = self.handle_line(raw_line)
            if reply is None:
                continue
            self._writer.write(reply + "\n")
            self._writer.flush()


__all__ = ["LineTransport", "Dispatcher"]
