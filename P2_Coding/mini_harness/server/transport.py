"""传输层 —— 换行 JSON-RPC 的收发（stdio、TCP、测试共用一条路径）。

一行进、一行出：

    serve_forever()   从 reader 逐行读帧 → parse_line → service.dispatch
                      → 请求回 result/error 帧；通知不回（协议规定）
    读到 EOF          正常收尾（连接断开）

**`_11` 的变化**（事件流跟随需要"服务端主动推帧"）：

1. `dispatch(..., send=self.send)`：transport 把自己的"写一帧"回调交给服务层——
   `session.follow` 的重放与实时推送都走它，与响应帧共用同一个写路径
   （写出顺序 = 调用顺序，重放先于响应、事件按 seq 递增，天然有序）。
2. **写锁**：推送可能来自另一个线程（TCP 下 `on_event` 在会话线程里 flush），
   与响应帧的写入互斥，保证一行写完整、不交错。
3. **断开即注销**：`serve_forever` 结束时回调 `on_close`（serve 侧接
   `service.drop_sender`），断开的连接不会留下死订阅。

错误翻译（本层的职责就是把异常变成**稳定形状**的错误帧）：

    parse_line 抛 RpcError         → 按它自己的码回错误帧（id 尽力带回）
    service.dispatch 抛 RpcError   → 按它自己的码回错误帧（方法/参数问题由服务层定码）
    service.dispatch 抛其它异常     → INTERNAL_ERROR（-32603）+ 异常消息（不让服务进程崩）

**id 的尽力归属**：按 JSON-RPC 规范，解析错误无法知道 id 时应回 `id: null`；但
"帧能解析、只是字段不合法"（如 params 不是对象）时应尽量带回 id，客户端才能把
错误配对到它发出的请求。parse_line 出错后我们做一次**宽松提取**（能取到合法 id 就取）。

**通道纪律**：只写 reader/writer 给定的流；banner/日志这类人看的东西不归本层
（`serve.py` 写 stderr）。测试因此可以用 StringIO 驱动整条收发路径——与真实
stdio/TCP 完全同构（这正是 transport 独立成层的价值）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
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
    """传输层对服务的唯一要求：dispatch(method, params) -> 可序列化结果。

    实现可选择接收 `send`（"往当前连接写一帧"的回调）——`session.follow`
    用它推事件；不认识的实现可以只实现两参签名（Python 的鸭子类型下，
    transport 只在需要时传 keyword）。
    """

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


def _dispatch_with_send(service: Any, method: str, params: dict[str, Any], send: Callable) -> Any:
    """兼容两种 dispatcher：认识 send 的（服务层）传它；不认识的按两参调。"""
    try:
        return service.dispatch(method, params, send=send)
    except TypeError as exc:
        if "send" not in str(exc):
            raise
        return service.dispatch(method, params)


class LineTransport:
    """换行 JSON-RPC 传输：从 reader 收帧、把结果（与推送）写回 writer。"""

    def __init__(
        self,
        reader: TextIO,
        writer: TextIO,
        service: Dispatcher,
        *,
        on_close: Callable[[Callable[[str], None]], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._service = service
        self._on_close = on_close
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 写（响应帧与推送帧共用；带锁）
    # ------------------------------------------------------------------

    def send(self, frame_line: str) -> None:
        """把一个已序列化的帧写到连接上（加换行、立即 flush）。"""
        with self._write_lock:
            self._writer.write(frame_line + "\n")
            self._writer.flush()

    # ------------------------------------------------------------------
    # 单行（可直接单测；serve_forever 只是它的循环壳）
    # ------------------------------------------------------------------

    def handle_line(self, line: str) -> str | None:
        """处理一行；请求返回响应帧文本，通知返回 None。异常不外抛。

        注意：主动推送（session.follow 的重放/实时事件）**不经返回值**——
        它们通过 self.send 直达连接，所以这里返回的只是"响应帧或 None"。
        """
        try:
            frame = parse_line(line)
        except RpcError as exc:
            return self._error_reply(_salvage_id(line), exc)

        if isinstance(frame, Request):
            return self._handle_request(frame)
        # 通知：分派但**不回响应**（协议规定）；分派异常同样静默丢弃。
        try:
            _dispatch_with_send(self._service, frame.method, frame.params, self.send)
        except Exception:  # noqa: BLE001 —— 通知的错误无处可回，按协议丢弃
            pass
        return None

    def _handle_request(self, request: Request) -> str:
        try:
            result = _dispatch_with_send(self._service, request.method, request.params, self.send)
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
        try:
            for raw_line in self._reader:
                if not raw_line.strip():
                    if raw_line == "":
                        break
                reply = self.handle_line(raw_line)
                if reply is None:
                    continue
                self.send(reply)
        finally:
            if self._on_close is not None:
                self._on_close(self.send)


__all__ = ["LineTransport", "Dispatcher"]
