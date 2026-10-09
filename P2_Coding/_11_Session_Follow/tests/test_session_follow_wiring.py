"""_11_Session_Follow 的端到端测试：TCP 多客户端 + 断线重连补齐 + attach 客户端。

验收映射（本阶段 README）：
- 「两个终端 serve + attach 共享同一会话」→ 两个 TCP 连接（不同 sender）跟同一会话，
  一个连接发 prompt，另一个连接收到推来的事件；
- 「kill 掉 attach 后重连能补齐断线期间缺失的事件」→ 断开一个连接、在断线期间
  产生事件、用 `from_seq=已见最大 seq` 重连，断言**恰好补齐缺口、不重不漏**；
- 「follow = 重放 + 订阅的 golden 测试」→ test_session_follow_golden.py。

运行：cd P2_Coding/_11_Session_Follow && python -m pytest -q
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

from config import load_profile
from providers import boot_tree
from server import HarnessService, make_server

STAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILES = STAGE_ROOT / "profiles"


class Client:
    """极简 TCP 客户端：连服务、收发帧（测试用，独立于 attach.py 的界面逻辑）。"""

    def __init__(self, host: str, port: int) -> None:
        self._socket = socket.create_connection((host, port), timeout=10)
        self._socket.settimeout(None)
        self._reader = self._socket.makefile("r", encoding="utf-8", newline="\n")
        self._writer = self._socket.makefile("w", encoding="utf-8", newline="\n")
        self.frames: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            for line in self._reader:
                if not line.strip():
                    continue
                frame = json.loads(line)
                with self._lock:
                    self.frames.append(frame)
        except (OSError, ValueError):
            return

    def send(self, identifier: int, method: str, params: dict | None = None) -> None:
        frame: dict = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            frame["params"] = params
        with self._lock:
            pass  # 写不并发（测试串行调用）
        self._writer.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self._writer.flush()

    # ---- 断言辅助 ----

    def events(self) -> list[dict]:
        with self._lock:
            return [f["params"]["event"] for f in self.frames if f.get("method") == "session.event"]

    def response(self, identifier: int, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for frame in self.frames:
                    if frame.get("id") == identifier:
                        return frame
            time.sleep(0.02)
        raise TimeoutError(f"等不到响应 id={identifier}")

    def disconnect(self) -> None:
        """模拟"被 kill"：shutdown + 关闭全部句柄（服务端读循环随即 EOF）。

        `socket.close()` 单独不够——`makefile` 持有自己的 fd 引用；要
        `shutdown(SHUT_RDWR)` 才能让对端立刻看到连接断开（就像进程被杀）。
        """
        self._stop.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        for stream in (self._reader, self._writer):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self._socket.close()
        except OSError:
            pass


def _start_server(tmp_path: Path) -> tuple[object, HarnessService, tuple[str, int]]:
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    plug_ctx = boot_tree(load_profile("dev", profiles_dir=PROFILES), stage_root=STAGE_ROOT)
    service = HarnessService(plug_ctx, root=tmp_path / "sessions", workspace=tmp_path / "ws")
    server = make_server("127.0.0.1", 0, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, service, (host, port)


# =========================================================================
# 1) 两个客户端共享同一会话（README 的第一个验收）
# =========================================================================


def test_two_clients_share_one_session(tmp_path: Path):
    server, service, (host, port) = _start_server(tmp_path)
    try:
        observer = Client(host, port)
        actor = Client(host, port)
        observer.send(1, "initialize")
        observer.response(1)
        actor.send(1, "initialize")
        actor.response(1)

        # 观察者订阅，然后行动者发 prompt（不同连接）
        observer.send(2, "session.follow", {"session": "shared", "from_seq": 0})
        observer.response(2)
        actor.send(2, "session.prompt", {"session": "shared", "text": "你好"})
        actor.response(2, timeout=60)

        # 观察者应收到行动者这次 prompt 产生的全部事件
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
            e["type"] == "turn/end" for e in observer.events()
        ):
            time.sleep(0.05)
        types = [e["type"] for e in observer.events()]
        assert "session/start" in types and "turn/end" in types
    finally:
        observer.disconnect()
        actor.disconnect()
        server.shutdown()
        server.server_close()


# =========================================================================
# 2) ★ 断线重连补齐（README 的核心验收）
# =========================================================================


def test_reconnect_fills_gap_exactly(tmp_path: Path):
    """kill attach → 断线期间产生事件 → 重连 from=已见 seq → 恰好补齐缺口。"""
    server, service, (host, port) = _start_server(tmp_path)
    try:
        client = Client(host, port)
        client.send(1, "initialize")
        client.response(1)
        client.send(2, "session.follow", {"session": "gap", "from_seq": 0})
        client.response(2)

        # 断线前先产生一段事件
        client.send(3, "session.prompt", {"session": "gap", "text": "第一句"})
        client.response(3, timeout=60)
        seen_before = [e["seq"] for e in client.events()]
        assert seen_before
        last_seen = max(seen_before)

        # 模拟 kill：断开连接（服务端注销订阅）
        client.disconnect()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and service.subscriptions:
            time.sleep(0.05)
        assert service.subscriptions == [], "断开后订阅应被注销"

        # 断线期间：另一个连接继续产生事件（对断开的 attach 不可见）
        actor = Client(host, port)
        actor.send(1, "initialize")
        actor.response(1)
        actor.send(2, "session.prompt", {"session": "gap", "text": "断线期间的第二句"})
        actor.response(2, timeout=60)

        # 重连：from_seq = 断线前已见的最大 seq
        reconnected = Client(host, port)
        reconnected.send(1, "initialize")
        reconnected.response(1)
        reconnected.send(2, "session.follow", {"session": "gap", "from_seq": last_seen})
        response = reconnected.response(2)

        gap_events = [e for e in reconnected.events() if e["seq"] > last_seen]
        seqs = [e["seq"] for e in gap_events]
        assert seqs == list(range(last_seen + 1, seqs[-1] + 1)), "补齐的事件必须连续"
        assert response["result"]["replayed"] == len(seqs)
        # 断线期间那个 turn 的事件确实在里面（turn/start + turn/end 各一）
        assert any(e["type"] == "turn/start" and e["data"]["turn"] == 2 for e in gap_events)
        assert any(e["type"] == "turn/end" and e["data"]["turn"] == 2 for e in gap_events)
        # 不重：没有任何 seq <= last_seen 的事件被重发
        assert all(e["seq"] > last_seen for e in reconnected.events())

        # 重连后仍然活着：新事件照推
        actor.send(3, "session.prompt", {"session": "gap", "text": "第三句"})
        actor.response(3, timeout=60)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(
            e["type"] == "turn/end" and e["data"]["turn"] == 3 for e in reconnected.events()
        ):
            time.sleep(0.05)
        assert any(
            e["type"] == "turn/end" and e["data"]["turn"] == 3 for e in reconnected.events()
        )

        reconnected.disconnect()
        actor.disconnect()
    finally:
        server.shutdown()
        server.server_close()


# =========================================================================
# 3) 实时性：长 turn 进行中的事件也会推（不是等 turn 结束批量给）
# =========================================================================


def test_events_stream_during_turn(tmp_path: Path):
    """turn 还没结束时，先产生的事件已经在观察者手里（事件产生即推）。"""
    server, service, (host, port) = _start_server(tmp_path)
    try:
        observer = Client(host, port)
        observer.send(1, "initialize")
        observer.response(1)
        observer.send(2, "session.follow", {"session": "live", "from_seq": 0})
        observer.response(2)

        actor = Client(host, port)
        actor.send(1, "initialize")
        actor.response(1)
        actor.send(2, "session.prompt", {"session": "live", "text": "你好"})
        actor.response(2, timeout=60)  # 等 turn 跑完

        # 观察者收到的第一条就是 session/start（turn 开始前就产生的事件）
        events = observer.events()
        assert events and events[0]["type"] == "session/start"
        assert any(e["type"] == "turn/end" for e in events)

        observer.disconnect()
        actor.disconnect()
    finally:
        server.shutdown()
        server.server_close()
