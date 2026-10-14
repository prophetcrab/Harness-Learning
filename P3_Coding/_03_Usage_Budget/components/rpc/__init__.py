"""rpc 构件 —— 换行 JSON-RPC 2.0 的帧词汇与传输（纯逻辑，不认识插件）。

两部分：
    protocol.py   帧解析 / 构造 / 稳定错误码
    transport.py  LineTransport（收发循环）+ Channel（每连接发送通道）

服务端在 `plugins/rpc.py`（RpcService：initialize / session.prompt /
session.follow）；本包只关心"字节 ↔ 帧"和"异常 ↔ 错误帧"。
"""

from components.rpc.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PARSE_ERROR,
    Frame,
    Notification,
    Request,
    RpcError,
    error_frame,
    notification_frame,
    parse_line,
    result_frame,
)
from components.rpc.transport import Channel, Dispatcher, LineTransport

__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "METHOD_NOT_FOUND",
    "NOT_INITIALIZED",
    "PARSE_ERROR",
    "Channel",
    "Dispatcher",
    "Frame",
    "LineTransport",
    "Notification",
    "Request",
    "RpcError",
    "error_frame",
    "notification_frame",
    "parse_line",
    "result_frame",
]
