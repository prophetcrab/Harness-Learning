"""协议层 —— 换行 JSON-RPC 2.0 的帧词汇（解析 / 构造 / 稳定错误码）。

沿用 P2 `_10` 的协议形状（同一套错误码、同一套帧构造），P3 里它落在**构件层**：
纯逻辑，不认识插件、不认识 agent——服务端（`plugins/rpc.py`）与测试都只依赖它。

每行一帧：

    请求      {"jsonrpc": "2.0", "id": 1, "method": "...", "params": {...}}
    响应      {"jsonrpc": "2.0", "id": 1, "result": {...}}
    错误响应  {"jsonrpc": "2.0", "id": 1, "error": {"code": ..., "message": ...}}
    通知      {"jsonrpc": "2.0", "method": "...", "params": {...}}      （无 id，不回）

错误码（标准段 + server-defined 段）：

    -32700  Parse error       行不是合法 JSON
    -32600  Invalid request   是 JSON 但不是合法请求帧
    -32601  Method not found  方法不认识（服务层用）
    -32602  Invalid params    参数不合规（服务层用）
    -32603  Internal error    处理中抛异常（服务层用）
    -32002  Not initialized   server-defined：未握手就调用其它方法

**通道纪律**（stdio 服务的经典坑）：stdout 是**协议线**——只许写帧；人看的
banner、日志一律走 stderr。这样客户端把每一行都当帧解析就是安全的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JSONRPC_VERSION = "2.0"

# ---------------------------------------------------------------------------
# 稳定错误码
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: server-defined 段（-32000..-32099）：未握手就调用其它方法。
NOT_INITIALIZED = -32002


class RpcError(Exception):
    """服务层/协议层的可上报错误：稳定码 + 人话消息（+ 可选结构化 data）。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# 帧词汇
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """请求帧：id 必有（回响应靠它配对）。"""

    id: int | str
    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Notification:
    """通知帧：无 id（不回响应）。"""

    method: str
    params: dict[str, Any] = field(default_factory=dict)


Frame = Request | Notification


def parse_line(line: str) -> Frame:
    """把一行文本解析成 Request 或 Notification；非法时抛 RpcError。"""
    text = line.strip()
    if not text:
        raise RpcError(PARSE_ERROR, "空行不是合法帧")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RpcError(PARSE_ERROR, f"不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise RpcError(INVALID_REQUEST, f"帧必须是 JSON 对象，收到 {type(raw).__name__}")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(
            INVALID_REQUEST,
            f"jsonrpc 字段必须是 {JSONRPC_VERSION!r}（收到 {raw.get('jsonrpc')!r}）",
        )
    method = raw.get("method")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_REQUEST, "params 必须是对象（或省略）")
    if "id" in raw:
        identifier = raw["id"]
        if not isinstance(identifier, (int, str)) or isinstance(identifier, bool):
            raise RpcError(INVALID_REQUEST, "id 必须是数字或字符串")
        if not isinstance(method, str) or not method:
            raise RpcError(INVALID_REQUEST, "请求缺少 method")
        return Request(id=identifier, method=method, params=params)
    # 无 id = 通知
    if not isinstance(method, str) or not method:
        raise RpcError(INVALID_REQUEST, "通知缺少 method")
    return Notification(method=method, params=params)


# ---------------------------------------------------------------------------
# 帧构造（统一从这里出，保证序列化形状稳定）
# ---------------------------------------------------------------------------


def _dump(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False)


def result_frame(request_id: int | str, result: Any) -> str:
    """构造一行响应帧（id 原样带回）。"""
    return _dump({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result})


def error_frame(
    request_id: int | str | None, code: int, message: str, data: Any = None
) -> str:
    """构造一行错误响应帧；解析失败时 id 允许为 None（协议规定）。"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _dump({"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error})


def notification_frame(method: str, params: dict[str, Any] | None = None) -> str:
    """构造一行通知帧（无 id）。"""
    frame: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        frame["params"] = params
    return _dump(frame)


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "METHOD_NOT_FOUND",
    "NOT_INITIALIZED",
    "PARSE_ERROR",
    "Frame",
    "Notification",
    "Request",
    "RpcError",
    "error_frame",
    "notification_frame",
    "parse_line",
    "result_frame",
]
