"""P3 RPC 插件测试：握手门禁、prompt、follow（重放+订阅）、断线补齐、TCP。

"RPC 是插件"的验收形状：装载即起（TCP 可选）、卸载即停、订阅随连接注销。
多数用例走内存流（StringIO）——与真实 stdio 完全同构，不需要子进程；
TCP 用例走真实 socket，验证多客户端共享同一会话。
"""

from __future__ import annotations

import io
import json
import socket
import threading
import urllib.error  # noqa: F401  （保持与其它测试一致的导入风格）
from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from components.rpc import LineTransport, RpcError
from plugins import (
    RPC_SERVICE,
    boot_offline,
    rpc_plugin,
)

# ---------------------------------------------------------------------------
# 测试脚手架：一个"客户端"= 一条内存连接
# ---------------------------------------------------------------------------


class MemoryClient:
    """内存客户端：帧写进 LineTransport 的 reader，读它的 writer（与 stdio 同构）。"""

    def __init__(self, service) -> None:
        self._pipe = io.StringIO()
        self._out = io.StringIO()
        self.transport = LineTransport(self._pipe, self._out, service)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        """发一次请求，返回最后一条响应帧（跳过中间的通知帧）。"""
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        )
        self.transport.handle_line(payload)
        frames = [json.loads(line) for line in self._out.getvalue().strip().splitlines()]
        returns = [f for f in frames if "id" in f and f["id"] == self._id]
        return returns[-1] if returns else frames[-1]

    def notifications(self, method: str = "session.event") -> list[dict]:
        frames = [json.loads(line) for line in self._out.getvalue().strip().splitlines()]
        return [f for f in frames if f.get("method") == method]

    def disconnect(self) -> None:
        self.transport.serve_forever()  # reader 已空 → 立即 EOF → drop_sender


def _boot(tmp_path: Path, script: list | None = None):
    return boot_offline(
        root=tmp_path / "sessions", workspace=tmp_path / "ws", script=script
    )


def _rpc(booted, **kwargs):
    booted.ctx.load_plugin(rpc_plugin(**kwargs))
    return booted.ctx.require(RPC_SERVICE)


# ---------------------------------------------------------------------------
# 握手与 prompt
# ---------------------------------------------------------------------------


def test_not_initialized_gate_and_idempotent_handshake(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    service = _rpc(booted)
    client = MemoryClient(service)
    # 手工驱动一行：handle_line 会把响应写进 client._out
    client.transport.handle_line(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.prompt", "params": {"text": "hi"}})
    )
    first = json.loads(client._out.getvalue().strip().splitlines()[-1])
    assert first["error"]["code"] == -32002

    caps1 = client.call("initialize")
    caps2 = client.call("initialize")  # 重复握手幂等
    assert caps1["result"] == caps2["result"]
    assert caps1["result"]["capabilities"]["methods"] == [
        "initialize",
        "session.prompt",
        "session.follow",
    ]
    assert "calculate" in caps1["result"]["capabilities"]["tools"]
    booted.close()


def test_prompt_runs_turn_and_reports_summary(tmp_path: Path):
    booted = _boot(
        tmp_path,
        [
            tool_call_reply("calculate", {"expression": "2+3"}),
            text_reply("2+3=5。"),
        ],
    )
    service = _rpc(booted)
    client = MemoryClient(service)
    client.call("initialize")
    result = client.call("session.prompt", {"session": "s1", "text": "帮我算 2+3"})["result"]
    assert result["session"] == "s1"
    assert result["turn"] == 1 and result["status"] == "done"
    assert result["steps"] == 2 and result["final_text"] == "2+3=5。"
    booted.close()


def test_prompt_validates_params(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    service = _rpc(booted)
    client = MemoryClient(service)
    client.call("initialize")
    missing_text = client.call("session.prompt", {"session": "s1"})
    assert missing_text["error"]["code"] == -32602
    bad_session = client.call("session.prompt", {"session": "", "text": "hi"})
    assert bad_session["error"]["code"] == -32602
    unknown = client.call("session.nope")
    assert unknown["error"]["code"] == -32601
    booted.close()


# ---------------------------------------------------------------------------
# follow：重放 + 订阅
# ---------------------------------------------------------------------------


def test_follow_replays_existing_then_streams_live(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("第一次回答"), text_reply("第二次回答")])
    service = _rpc(booted)
    client = MemoryClient(service)
    client.call("initialize")

    # 先产生历史（不经过 RPC）：follow 应把它们全部重放出来。
    booted.agent.ask("第一问", session_id="s1")
    reply = client.call("session.follow", {"session": "s1", "from_seq": 0})["result"]
    assert reply["replayed"] > 0
    replayed = client.notifications()
    assert [n["params"]["seq"] for n in replayed] == list(range(1, reply["replayed"] + 1))
    assert reply["last_seq"] == replayed[-1]["params"]["seq"]

    # 再跑一个 turn：订阅者实时收到新事件（长 turn 中边跑边推）。
    before = len(client.notifications())
    client.call("session.prompt", {"session": "s1", "text": "第二问"})
    live = client.notifications()[before:]
    assert live, "prompt 期间应有实时推送"
    assert all(n["params"]["session"] == "s1" for n in live)
    seqs = [n["params"]["seq"] for n in client.notifications()]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))  # 不重不漏
    booted.close()


def test_follow_from_seq_resumes_without_overlap(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("一"), text_reply("二")])
    service = _rpc(booted)
    first = MemoryClient(service)
    first.call("initialize")
    booted.agent.ask("第一问", session_id="s1")
    first.call("session.follow", {"session": "s1", "from_seq": 0})
    seen_max = max(n["params"]["seq"] for n in first.notifications())
    first.disconnect()  # 断开：订阅注销

    # 断开期间又产生了新事件（"离线期"）。
    booted.agent.ask("第二问", session_id="s1")

    second = MemoryClient(service)
    second.call("initialize")
    reply = second.call("session.follow", {"session": "s1", "from_seq": seen_max})["result"]
    fresh = [n["params"]["seq"] for n in second.notifications()]
    assert reply["replayed"] == len(fresh)
    assert all(seq > seen_max for seq in fresh)  # 只补缺口，不重发已见
    booted.close()


def test_follow_requires_channel_and_valid_params(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    service = _rpc(booted)
    client = MemoryClient(service)
    client.call("initialize")
    bad_seq = client.call("session.follow", {"session": "s1", "from_seq": -1})
    assert bad_seq["error"]["code"] == -32602
    with pytest.raises(RpcError):
        service.session_follow({"session": "s1"}, send=None)
    booted.close()


def test_subscription_dropped_on_disconnect_and_plugin_unload(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    service = _rpc(booted)
    client = MemoryClient(service)
    client.call("initialize")
    client.call("session.follow", {"session": "s1"})
    assert service.subscription_count == 1

    client.disconnect()  # EOF → drop_sender
    assert service.subscription_count == 0

    other = MemoryClient(service)
    other.call("initialize")
    other.call("session.follow", {"session": "s1"})
    assert service.subscription_count == 1
    booted.ctx.unload_plugin("rpc")  # 注册即 effect：订阅随插件回卷
    assert service.subscription_count == 0
    booted.close()


# ---------------------------------------------------------------------------
# TCP：装载即监听、多客户端共享同一会话、卸载即停（端口释放）
# ---------------------------------------------------------------------------


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _tcp_call(port: int, frames: list[dict], *, expect: int) -> list[dict]:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        stream = sock.makefile("rw", encoding="utf-8", newline="\n")
        for frame in frames:
            stream.write(json.dumps(frame) + "\n")
        stream.flush()
        replies: list[dict] = []
        for _ in range(expect):
            line = stream.readline()
            if not line:
                break
            replies.append(json.loads(line))
        return replies


def test_tcp_plugin_serves_two_clients_same_session(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("TCP 回答一"), text_reply("TCP 回答二")])
    service = _rpc(booted, listen=0)
    host, port = service.address
    assert _port_open(port)

    # 客户端 A：握手 + 第一问
    replies = _tcp_call(
        port,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "session.prompt",
             "params": {"session": "shared", "text": "第一问"}},
        ],
        expect=2,
    )
    assert replies[-1]["result"]["turn"] == 1

    # 客户端 B：握手 + 第二问——同一会话接续（多客户端共享 service）
    replies = _tcp_call(
        port,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "session.prompt",
             "params": {"session": "shared", "text": "第二问"}},
        ],
        expect=2,
    )
    assert replies[-1]["result"]["turn"] == 2

    booted.ctx.unload_plugin("rpc")
    assert not _port_open(port)  # 卸载即停：端口释放
    booted.close()


def test_tcp_follow_receives_pushes_from_another_connection(tmp_path: Path):
    """跨连接推送：B 连接订阅，A 连接 prompt——B 实时收到事件。"""
    booted = _boot(tmp_path, [text_reply("跨连接回答")])
    service = _rpc(booted, listen=0)
    _, port = service.address

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sub_sock:
        sub = sub_sock.makefile("rw", encoding="utf-8", newline="\n")

        def send(frame: dict) -> None:
            sub.write(json.dumps(frame) + "\n")
            sub.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert "result" in json.loads(sub.readline())
        send({"jsonrpc": "2.0", "id": 2, "method": "session.follow",
              "params": {"session": "s9"}})
        assert json.loads(sub.readline())["id"] == 2  # follow 的响应

        # 另一个连接发起 prompt（客户端 A）
        def asker() -> None:
            _tcp_call(
                port,
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "session.prompt",
                     "params": {"session": "s9", "text": "跨连接"}},
                ],
                expect=2,
            )

        worker = threading.Thread(target=asker)
        worker.start()
        # B 应收到若干 session.event 推送（第一条是 session/start）
        first_push = json.loads(sub.readline())
        worker.join(timeout=15)
        assert first_push["method"] == "session.event"
        assert first_push["params"]["session"] == "s9"
        assert first_push["params"]["type"] == "session/start"

    booted.ctx.unload_plugin("rpc")
    booted.close()


def test_stdio_mode_has_no_address_and_unloads_cleanly(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    service = _rpc(booted)  # listen=None：stdio 模式
    assert service.address is None
    booted.ctx.unload_plugin("rpc")
    assert RPC_SERVICE not in booted.ctx.services
    booted.close()
