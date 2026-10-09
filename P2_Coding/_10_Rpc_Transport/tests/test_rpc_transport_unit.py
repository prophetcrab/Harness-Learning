"""_10_Rpc_Transport 的单元测试：协议帧 + 传输循环（不碰 harness 业务）。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**里
最底的两层——`server/protocol.py`（帧的词汇）与 `server/transport.py`
（换行收发循环的错误翻译），用一个假的 dispatcher 驱动，不 boot 任何东西。

覆盖：
1. 帧解析：请求/通知区分；jsonrpc 版本、id 类型、params 类型的校验；错误码
2. 帧构造：result / error / notification 的稳定形状（golden 见 test_rpc_golden.py）
3. 传输循环：请求→响应、通知→不回、坏帧→-32700、id 的尽力归属、异常兜底
4. 空行/EOF：空行报 PARSE_ERROR、EOF 正常收尾

运行：cd P2_Coding/_10_Rpc_Transport && python -m pytest -q
"""

from __future__ import annotations

import io

import pytest

from server import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    NOT_INITIALIZED,
    PARSE_ERROR,
    LineTransport,
    Notification,
    Request,
    RpcError,
    error_frame,
    notification_frame,
    parse_line,
    result_frame,
)

# =========================================================================
# 1) 帧解析
# =========================================================================


def test_parse_request():
    frame = parse_line('{"jsonrpc":"2.0","id":7,"method":"initialize","params":{"a":1}}')
    assert isinstance(frame, Request)
    assert frame.id == 7 and frame.method == "initialize" and frame.params == {"a": 1}


def test_parse_request_without_params_defaults_empty():
    frame = parse_line('{"jsonrpc":"2.0","id":1,"method":"m"}')
    assert isinstance(frame, Request) and frame.params == {}


def test_parse_string_id():
    frame = parse_line('{"jsonrpc":"2.0","id":"abc","method":"m"}')
    assert isinstance(frame, Request) and frame.id == "abc"


def test_parse_notification():
    frame = parse_line('{"jsonrpc":"2.0","method":"m","params":{}}')
    assert isinstance(frame, Notification) and frame.method == "m"


def test_bad_json_is_parse_error():
    with pytest.raises(RpcError) as info:
        parse_line("not json")
    assert info.value.code == PARSE_ERROR


def test_non_object_frame_is_invalid_request():
    with pytest.raises(RpcError) as info:
        parse_line("[1, 2, 3]")
    assert info.value.code == INVALID_REQUEST


def test_wrong_version_is_invalid_request():
    with pytest.raises(RpcError, match="jsonrpc"):
        parse_line('{"jsonrpc":"1.0","id":1,"method":"m"}')


def test_bool_id_is_rejected():
    with pytest.raises(RpcError, match="id"):
        parse_line('{"jsonrpc":"2.0","id":true,"method":"m"}')


def test_non_object_params_is_rejected():
    with pytest.raises(RpcError, match="params"):
        parse_line('{"jsonrpc":"2.0","id":1,"method":"m","params":[1]}')


def test_missing_method_is_rejected():
    with pytest.raises(RpcError, match="method"):
        parse_line('{"jsonrpc":"2.0","id":1}')


# =========================================================================
# 2) 帧构造
# =========================================================================


def test_frame_constructors_shape():
    assert (
        result_frame(1, {"ok": True})
        == '{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}'
    )
    assert (
        error_frame(1, -32602, "参数不对")
        == '{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "参数不对"}}'
    )
    assert (
        notification_frame("session.event", {"seq": 5})
        == '{"jsonrpc": "2.0", "method": "session.event", "params": {"seq": 5}}'
    )


def test_error_frame_with_data_and_null_id():
    line = error_frame(None, PARSE_ERROR, "坏", data={"line": 3})
    assert '"id": null' in line and '"data": {"line": 3}' in line


def test_notification_frame_without_params():
    assert notification_frame("ping") == '{"jsonrpc": "2.0", "method": "ping"}'


# =========================================================================
# 3) 传输循环（假 dispatcher）
# =========================================================================


class FakeDispatcher:
    """最小 dispatcher：echo 方法；boom 抛异常；notify_calls 记录通知。"""

    def __init__(self) -> None:
        self.notify_calls: list[tuple[str, dict]] = []

    def dispatch(self, method: str, params: dict):
        if method == "echo":
            return {"echo": params}
        if method == "boom":
            raise RuntimeError("内部炸了")
        if method == "rpc_error":
            raise RpcError(INVALID_REQUEST, "服务层拒绝")
        if method == "tell":
            self.notify_calls.append((method, params))
            return None
        raise RpcError(-32601, f"未知方法：{method}")


def _run(lines: list[str]) -> list[str]:
    writer = io.StringIO()
    reader = io.StringIO("\n".join(lines) + "\n")
    LineTransport(reader, writer, FakeDispatcher()).serve_forever()
    return writer.getvalue().splitlines()


def test_request_gets_response():
    [line] = _run(['{"jsonrpc":"2.0","id":1,"method":"echo","params":{"x":9}}'])
    assert '"id": 1' in line and '"echo": {"x": 9}' in line


def test_notification_gets_no_response():
    dispatcher = FakeDispatcher()
    writer = io.StringIO()
    LineTransport(
        io.StringIO('{"jsonrpc":"2.0","method":"tell","params":{"a":1}}\n'), writer, dispatcher
    ).serve_forever()
    assert writer.getvalue() == ""
    assert dispatcher.notify_calls == [("tell", {"a": 1})]


def test_notification_error_is_swallowed():
    """通知的处理异常无处可回——按协议丢弃，不炸循环。"""
    dispatcher = FakeDispatcher()
    writer = io.StringIO()
    LineTransport(
        io.StringIO('{"jsonrpc":"2.0","method":"boom"}\n'), writer, dispatcher
    ).serve_forever()
    assert writer.getvalue() == ""


def test_bad_json_gets_parse_error_with_null_id():
    [line] = _run(["junk"])
    assert '"id": null' in line and f'"code": {PARSE_ERROR}' in line


def test_invalid_frame_salvages_id():
    """帧能解析出 id 但不合法 → 错误响应尽量带回 id（可配对）。"""
    [line] = _run(['{"jsonrpc":"2.0","id":42,"method":"echo","params":[1]}'])
    assert '"id": 42' in line and f'"code": {INVALID_REQUEST}' in line


def test_service_rpc_error_keeps_code():
    [line] = _run(['{"jsonrpc":"2.0","id":3,"method":"rpc_error"}'])
    assert '"id": 3' in line and f'"code": {INVALID_REQUEST}' in line


def test_unexpected_exception_becomes_internal_error():
    [line] = _run(['{"jsonrpc":"2.0","id":4,"method":"boom"}'])
    assert '"id": 4' in line and f'"code": {INTERNAL_ERROR}' in line
    assert "内部炸了" in line


def test_unknown_method_returns_not_found_from_dispatcher():
    [line] = _run(['{"jsonrpc":"2.0","id":5,"method":"nope"}'])
    assert '"code": -32601' in line


def test_empty_line_is_parse_error():
    [line] = _run([""])
    assert f'"code": {PARSE_ERROR}' in line


def test_batch_of_mixed_frames():
    lines = _run(
        [
            '{"jsonrpc":"2.0","id":1,"method":"echo","params":{}}',
            "garbage",
            '{"jsonrpc":"2.0","method":"tell"}',
            '{"jsonrpc":"2.0","id":2,"method":"echo","params":{"n":2}}',
        ]
    )
    assert len(lines) == 3  # 通知不回
    assert '"id": 1' in lines[0] and f'"code": {PARSE_ERROR}' in lines[1] and '"id": 2' in lines[2]


def test_not_initialized_code_is_stable():
    """server-defined 错误码 -32002 的取值被钉住（客户端可依赖）。"""
    assert NOT_INITIALIZED == -32002
