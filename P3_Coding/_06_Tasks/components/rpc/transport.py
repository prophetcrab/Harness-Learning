"""传输层 —— LineTransport：换行 JSON-RPC 的收发循环 + 每连接发送通道。

一行进、一行出。错误翻译（本层的全部职责）：把异常变成**稳定形状**的错误帧——

    parse_line 抛 RpcError        → 按它自己的码回错误帧（id 尽力带回）
    service.dispatch 抛 RpcError  → 按它自己的码回错误帧（方法/参数问题由服务层定码）
    service.dispatch 抛其它异常    → INTERNAL_ERROR（不让服务进程崩）

通知（无 id 的帧）分派但**不回响应**（协议规定）；分派异常同样静默丢弃。

**发送通道（Channel）**：一个连接的写端被包成一个小对象——`__call__(帧文本)`
→ 加锁写一行。它同时充当**订阅者身份**：RpcService 用同一个 Channel 对象区分
推送目标（`drop_sender(channel)` 注销该连接的全部订阅）。锁保证"turn 线程推送
事件"与"连接线程回帧"不会交错撕行。

EOF 即客户端断开：`serve_forever` 收尾时调 `service.drop_sender`，注销该连接的
订阅（订阅不泄漏）。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol, TextIO

from components.rpc.protocol import (
    INTERNAL_ERROR,
    Request,
    RpcError,
    error_frame,
    parse_line,
    result_frame,
)


class Channel:
    """一个连接的发送端：帧文本（不含换行）→ 加锁写出去。

    写入失败（客户端走了/套接字关了）**不抛异常**，而是把通道标记为关闭、
    静默丢弃后续帧——服务端推送是"尽力而为"：断线的订阅者由 transport 在
    EOF 时注销，不能因为一个连接断了就炸掉正在跑 turn 的线程。
    """

    def __init__(self, writer: TextIO) -> None:
        self._writer = writer
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __call__(self, frame_text: str) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._writer.write(frame_text + "\n")
                self._writer.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                # 连接已断（ValueError = 底层文件已关闭；OSError 覆盖各种套接字错误）。
                self._closed = True


class Dispatcher(Protocol):
    """传输层对服务的唯一要求：dispatch(method, params, send=通道) -> 可序列化结果。

    `send` 是服务端**主动推送**（如 `session.event` 通知）的出口；服务可选实现
    `drop_sender(channel)`，在连接断开时注销该连接登记过的一切。
    """

    def dispatch(self, method: str, params: dict[str, Any], *, send: Channel | None) -> Any:
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
    """换行 JSON-RPC 传输：从 reader 收帧、把结果与推送写回 writer。"""

    def __init__(self, reader: TextIO, writer: TextIO, service: Dispatcher) -> None:
        self._reader = reader
        self._service = service
        #: 本连接的发送通道（订阅身份用它做标识——同一个对象贯穿连接的一生）。
        self.channel = Channel(writer)

    # ------------------------------------------------------------------
    # 单行（可直接单测；serve_forever 只是它的循环壳）
    # ------------------------------------------------------------------

    def handle_line(self, line: str) -> None:
        """处理一帧：请求回响应（经 channel），通知不回。异常不外抛。"""
        try:
            frame = parse_line(line)
        except RpcError as exc:
            self.channel(error_frame(_salvage_id(line), exc.code, exc.message, exc.data))
            return

        if isinstance(frame, Request):
            self._handle_request(frame)
            return
        # 通知：分派但不回响应；分派异常静默丢弃（协议规定无处可回）。
        try:
            self._service.dispatch(frame.method, frame.params, send=self.channel)
        except Exception:  # noqa: BLE001
            pass

    def _handle_request(self, request: Request) -> None:
        try:
            result = self._service.dispatch(request.method, request.params, send=self.channel)
        except RpcError as exc:
            self.channel(error_frame(request.id, exc.code, exc.message, exc.data))
            return
        except Exception as exc:  # noqa: BLE001 —— 兜底：服务内部错误不让进程崩
            self.channel(error_frame(request.id, INTERNAL_ERROR, f"服务内部错误：{exc}"))
            return
        self.channel(result_frame(request.id, result))

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def serve_forever(self) -> None:
        """逐行读帧直到 EOF；收尾时注销本连接的订阅。"""
        try:
            for raw_line in self._reader:
                self.handle_line(raw_line)
        finally:
            drop = getattr(self._service, "drop_sender", None)
            if drop is not None:
                drop(self.channel)


__all__ = ["Channel", "Dispatcher", "LineTransport"]
