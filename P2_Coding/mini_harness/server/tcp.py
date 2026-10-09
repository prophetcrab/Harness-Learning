"""TCP 传输 —— 多个客户端连同一个常驻服务（事件流跟随的进程间形态）。

stdio 只能服务一个客户端（一个进程一条连接）；`_11` 的"两个终端 serve + attach
共享同一会话"需要**多客户端**。做法很小：TCP 上的每个 socket 连接都起一个
`LineTransport`（复用全部既有收发/错误/推送逻辑），共享**同一个** HarnessService
——会话表与订阅表在服务里，天然跨连接：A 连接产生的会话事件会推给 B 连接的订阅者。

实现要点：

1. **一连接一线程**：`socketserver.ThreadingTCPServer`，每连接一个 handler；
   handler 把 socket 包成 **UTF-8 文本流**后交给 `LineTransport.serve_forever`。
2. **断开即注销订阅**：transport 的 `on_close` 回调服务侧的 `drop_sender`——
   被杀掉的 attach 不会在服务里留下死订阅（这也是"重连补齐"成立的前提：
   重连的是一个全新连接/全新订阅，缺口靠 `from_seq` 重放补上，不靠旧订阅续命）。
3. **连接计数**（观测/测试用）：`TcpServer.connection_count`。

线程安全由两层保证：`HarnessService` 内部用可重入锁串行化 dispatch 与 flush；
`LineTransport` 的写有写锁（推送与响应不交错）。同一连接内的帧仍然串行处理
（handler 单线程读循环）。
"""

from __future__ import annotations

import socketserver
import threading
from typing import Any

from server.transport import LineTransport


class _Handler(socketserver.StreamRequestHandler):
    """一个 TCP 连接的收发循环：包一条 LineTransport，与 stdio 同路径。"""

    def setup(self) -> None:
        super().setup()
        # 二进制流包成 UTF-8 文本（与 stdio 的 sys.stdin/stdout 同形）。
        self.rfile = self.connection.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.connection.makefile("w", encoding="utf-8", newline="\n")

    def handle(self) -> None:
        server: TcpServer = self.server  # type: ignore[assignment]
        transport = LineTransport(
            self.rfile,  # type: ignore[arg-type]  —— 已在 setup() 包成文本流
            self.wfile,  # type: ignore[arg-type]
            server.service,
            on_close=server.on_connection_closed,  # 断开即注销该连接的订阅
        )
        server._note_opened()
        try:
            transport.serve_forever()  # 读帧、派发、写响应/推送，直到对端断开
        finally:
            server._note_closed()


class TcpServer(socketserver.ThreadingTCPServer):
    """多线程 TCP 服务器：把每条连接交给 LineTransport（共享同一 service）。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: Any) -> None:
        self.service = service
        self._connection_count = 0
        self._count_lock = threading.Lock()
        super().__init__(address, _Handler)

    # 连接计数（观测/测试用） -------------------------------------------------

    def _note_opened(self) -> None:
        with self._count_lock:
            self._connection_count += 1

    def _note_closed(self) -> None:
        with self._count_lock:
            self._connection_count = max(0, self._connection_count - 1)

    @property
    def connection_count(self) -> int:
        with self._count_lock:
            return self._connection_count

    def on_connection_closed(self, send: Any) -> None:
        """连接断开：注销它的订阅（由 transport 的 on_close 调用）。

        幂等：`drop_sender` 只是过滤订阅列表，重复调用无副作用（计数不在这里
        减——它由 handler 的 finally 管，保证恰好一次）。
        """
        drop = getattr(self.service, "drop_sender", None)
        if callable(drop):
            drop(send)


def make_server(host: str, port: int, service: Any) -> TcpServer:
    """构造（未启动的）TCP 服务器；`port=0` 时由系统分配（测试读 `server_address`）。"""
    return TcpServer((host, port), service)


__all__ = ["TcpServer", "make_server"]
