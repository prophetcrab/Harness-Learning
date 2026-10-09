"""attach.py —— 跟随客户端：订阅会话事件流、边看边发、断线重连自动补齐。

这是 `_11` 的客户端侧（与 `serve.py` 配对）。它把"follow = 重放 + 订阅"用在
人身上：

    python attach.py --session s1              # stdio：自己拉起 serve.py 子进程
    python attach.py --connect 8765 --session s1   # TCP：连到常驻服务

连上之后：

1. `initialize` 握手 → `session.follow(from_seq=--from)`：
   先收到**重放**（`seq > from` 的既有事件），随后**实时**事件随会话产生推来；
2. 事件按人看的格式流式渲染（turn 边界、消息、工具结果、提示词更新）；
3. 你在提示符里输入 → 作为 `session.prompt` 发给服务（与跟随同一条连接）；
4. **断线重连**：连接断了自动重连，重连时用"已见到的最大 seq"作为 `from_seq`
   ——断线期间产生的事件全部由服务端的**重放**补齐，接缝处不重不漏
   （这正是"日志是唯一真相"的红利：补缺不需要另建缓存，重放日志即可）。

命令：`/exit` 退出；`/seq` 显示当前已见的最大 seq（重连起点）。
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

HERE = Path(__file__).resolve().parent

#: 每个事件类型渲染成一行人话（消费侧展示；协议里是结构化 JSON）。


def itertools_count():
    """自增计数器（每次调用返回下一个整数）。"""
    value = 0

    def next_value() -> int:
        nonlocal value
        value += 1
        return value

    return next_value


def render_event(event: dict[str, Any], *, verbose: bool = False) -> str | None:
    """把一个事件渲染成一行人话；返回 None 表示这类事件默认不显示。"""
    event_type = event.get("type", "?")
    data = event.get("data", {})
    prefix = f"#{event.get('seq', '?'):>3} "
    if event_type == "turn/start":
        return f"\n{prefix}── turn {data.get('turn')} ──"
    if event_type == "user/message":
        return f"{prefix}你：{data.get('content', '')}"
    if event_type == "assistant/message":
        content = data.get("content") or ""
        calls = data.get("tool_calls") or []
        lines = []
        if content:
            lines.append(f"{prefix}助手：{content}")
        if calls:
            names = "、".join(call.get("name", "?") for call in calls)
            lines.append(f"{prefix}（申请调用工具：{names}）")
        return "\n".join(lines) if lines else None
    if event_type == "tool/result":
        mark = "✗" if data.get("is_error") else "✓"
        result = data.get("result")
        summary = json.dumps(result, ensure_ascii=False)
        if len(summary) > 160:
            summary = summary[:160] + "…"
        return f"{prefix}{mark} {data.get('name')} → {summary}"
    if event_type == "turn/end":
        return f"{prefix}（turn {data.get('turn')} 结束：{data.get('status')}）"
    if event_type == "system/message":
        return f"{prefix}· 系统提示词更新（渲染有变化）" if verbose else None
    if event_type == "session/start":
        return f"{prefix}· 会话开始（system 提示已记录）" if verbose else None
    return f"{prefix}· {event_type}" if verbose else None


class AttachClient:
    """一条连接 + 跟随状态。可断线重连（TCP）；stdio 下服务随客户端进程。

    线程模型：读线程持续消费服务端帧（事件/响应），主线程读用户输入；
    写出（follow/prompt）加锁——同一连接不并发写。
    """

    def __init__(
        self,
        *,
        session: str,
        from_seq: int = 0,
        connect: str | None = None,
        serve_args: list[str] | None = None,
        verbose: bool = False,
        on_event: Callable[[str], None] | None = None,
        auto_reconnect: bool = True,
        reconnect_attempts: int = 20,
        reconnect_delay: float = 0.3,
    ) -> None:
        self.session = session
        self.last_seq = from_seq
        self.verbose = verbose
        self._connect_target = connect
        self._serve_args = serve_args or []
        self._on_event = on_event
        self._auto_reconnect = auto_reconnect
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_delay = reconnect_delay

        self._reader: TextIO | None = None
        self._writer: TextIO | None = None
        self._process: subprocess.Popen | None = None
        self._socket: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = itertools_count()
        self._responses: dict[int, dict[str, Any]] = {}
        self._response_event = threading.Event()
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self.reconnects = 0  # 重连次数（观测/测试用）

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _open(self) -> tuple[TextIO, TextIO]:
        """建一条连接：TCP 或 stdio（拉起 serve.py 子进程）。"""
        if self._connect_target:
            host, _, port = self._connect_target.rpartition(":")
            sock = socket.create_connection((host or "127.0.0.1", int(port)), timeout=10)
            sock.settimeout(None)
            self._socket = sock
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            writer = sock.makefile("w", encoding="utf-8", newline="\n")
            return reader, writer
        command = [sys.executable, str(HERE / "serve.py"), *self._serve_args]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # 服务端 banner 直接透到本进程 stderr（人不看协议线）
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        assert self._process.stdout is not None and self._process.stdin is not None
        return self._process.stdout, self._process.stdin

    def _close(self) -> None:
        """收尾顺序很重要（否则会挂住）：

        1. **TCP**：先 `shutdown(SHUT_RDWR)`——它会唤醒正阻塞在 recv 上的读线程
           （读了会抛错、线程退出）；反过来先 `reader.close()` 会和阻塞读抢锁
           （Windows 上实测死锁）。
        2. **stdio**：先关**写流**（子进程 stdin）→ 服务端读到 EOF 正常退出 →
           等子进程结束（读流随之 EOF，读线程自己退出）。
        3. 最后再关读流 / socket 本体。
        """
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            if self._writer is not None:
                self._writer.close()
        except OSError:
            pass
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.terminate()
        try:
            if self._reader is not None:
                self._reader.close()
        except (OSError, ValueError):
            pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 收发
    # ------------------------------------------------------------------

    def _send(self, method: str, params: dict[str, Any]) -> int:
        """发一个请求，返回 id（响应异步到达，放 self._responses）。"""
        with self._write_lock:
            if self._writer is None:
                raise ConnectionError("连接不可用")
            identifier = self._next_id()
            frame = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            self._writer.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self._writer.flush()
            return identifier

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        if frame.get("method") == "session.event":
            params = frame.get("params", {})
            if params.get("session") != self.session:
                return
            event = params.get("event", {})
            seq = int(event.get("seq", 0))
            with self._state_lock:
                if seq > self.last_seq:
                    self.last_seq = seq
            line = render_event(event, verbose=self.verbose)
            if line is not None:
                print(line, flush=True)
            if self._on_event is not None:
                self._on_event(line or "")
            return
        identifier = frame.get("id")
        if isinstance(identifier, int):
            self._responses[identifier] = frame
            self._response_event.set()

    def _reader_loop(self) -> None:
        """读线程：消费帧；连接断开则尝试重连（重连后 follow from=last_seq 补齐）。

        静默收尾的条件：stop() 之后（用户主动退出）无论读到什么都安静退出——
        关流会让阻塞读抛 OSError/ValueError（"I/O operation on closed file"），
        这些在"正在关闭"语境下都是预期内的，不该冒成线程异常。
        """
        while not self._stop.is_set():
            try:
                reader = self._reader
                if reader is None:
                    return
                for line in reader:
                    if self._stop.is_set():
                        return
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        print(f"[attach] 收到非 JSON 行（忽略）：{line[:80]!r}", file=sys.stderr)
                        continue
                    self._handle_frame(frame)
                raise ConnectionError("连接被对端关闭")
            except (OSError, ValueError, ConnectionError):
                if self._stop.is_set():
                    return  # 主动退出：关流引发的读错误是预期内的
                if not self._auto_reconnect or not self._connect_target:
                    print("\n[attach] 连接断开（未启用重连或 stdio 模式），退出。", file=sys.stderr)
                    return
                if not self._reconnect():
                    return

    def _reconnect(self) -> bool:
        """重连 + 从 last_seq 补齐。成功返回 True。

        本方法在读线程里被调用——**不能在这里等响应**（读响应的人就是自己）。
        只发 initialize + follow，重放事件与两份响应都由随后的读循环自然处理
        （follow 重放先到、响应随后，与首次连接完全同序）。
        """
        print(f"\n[attach] 连接断开，重连中…（将从 seq>{self.last_seq} 补齐）", file=sys.stderr)
        for _ in range(self._reconnect_attempts):
            if self._stop.is_set():
                return False
            try:
                self._close()
                self._reader, self._writer = self._open()
            except OSError:
                time.sleep(self._reconnect_delay)
                continue
            self.reconnects += 1
            self._send("initialize", {})
            self._send("session.follow", {"session": self.session, "from_seq": self.last_seq})
            print(f"[attach] 已重连（第 {self.reconnects} 次），缺口由服务端重放补齐。", file=sys.stderr)
            return True
        print("[attach] 重连失败，退出。", file=sys.stderr)
        return False

    def _wait_response(self, identifier: int, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self._responses.pop(identifier, None)
            if response is not None:
                return response
            self._response_event.wait(0.1)
            self._response_event.clear()
        raise TimeoutError(f"等待响应超时（id={identifier}）")

    # ------------------------------------------------------------------
    # 对外动作
    # ------------------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """连接 + 握手 + 跟随（重放会先于本方法返回到达）。

        注意顺序：**先启动读线程再发 initialize**——响应由读线程收下，
        主线程只是等待；反过来的话没人读响应，握手必然超时。
        """
        self._reader, self._writer = self._open()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        identifier = self._send("initialize", {})
        handshake = self._wait_response(identifier)
        if "error" in handshake:
            raise RuntimeError(f"握手失败：{handshake['error']}")
        self._send("session.follow", {"session": self.session, "from_seq": self.last_seq})
        return handshake["result"]

    def ask(self, text: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """发一条 prompt 并等待它的响应（响应回来时 turn 已跑完）。"""
        identifier = self._send("session.prompt", {"session": self.session, "text": text})
        return self._wait_response(identifier, timeout=timeout)

    def stop(self) -> None:
        self._stop.set()
        self._close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="attach.py",
        description="跟随客户端：订阅会话事件流（重放+实时）、可发送、断线自动补齐",
    )
    parser.add_argument("--session", "-s", default="default", help="要跟随的会话 id")
    parser.add_argument("--from", dest="from_seq", type=int, default=0, help="从 seq>N 开始（默认 0=全部）")
    parser.add_argument("--connect", default=None, help="连 TCP 服务：端口 或 host:端口（默认 stdio 拉起 serve.py）")
    parser.add_argument("--profile", default="dev", help="stdio 模式下 serve.py 的 profile")
    parser.add_argument("--root", default=None, help="stdio 模式下会话日志根目录")
    parser.add_argument("--workspace", default=None, help="stdio 模式下工作区")
    parser.add_argument("--auto-approve", action="store_true", help="stdio 模式下放开审批")
    parser.add_argument("--verbose", action="store_true", help="显示所有事件（含 system/step）")
    parser.add_argument("--ask", default=None, help="发一条消息后退出（非交互）")
    args = parser.parse_args(argv)

    serve_args = ["--profile", args.profile]
    if args.root:
        serve_args += ["--root", args.root]
    if args.workspace:
        serve_args += ["--workspace", args.workspace]
    if args.auto_approve:
        serve_args.append("--auto-approve")

    client = AttachClient(
        session=args.session,
        from_seq=args.from_seq,
        connect=args.connect,
        serve_args=serve_args,
        verbose=args.verbose,
    )
    handshake = client.start()
    tools = handshake["capabilities"]["tools"]
    print(f"[attach] 已连接（协议 {handshake.get('protocol')}）；工具：{tools}", file=sys.stderr)
    print(f"[attach] 跟随会话 {args.session!r}，从 seq>{args.from_seq} 开始。输入 /exit 退出。", file=sys.stderr)

    try:
        if args.ask is not None:
            response = client.ask(args.ask)
            result = response.get("result", {})
            print(f"\n助手> {result.get('final_text', '')}")
            return 0 if "error" not in response else 1
        while True:
            try:
                line = input("你> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = line.strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/seq":
                print(f"  已见最大 seq：{client.last_seq}（重连将从此继续）")
                continue
            try:
                response = client.ask(text)
            except TimeoutError as exc:
                print(f"[attach] {exc}", file=sys.stderr)
                continue
            if "error" in response:
                print(f"[attach] 服务返回错误：{response['error']}", file=sys.stderr)
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
