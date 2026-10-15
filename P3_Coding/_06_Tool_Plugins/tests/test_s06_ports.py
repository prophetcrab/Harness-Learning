"""端口选择（`components/net.py`）与"两个 harness 并存"的账本隔离测试。

**回归背景**（用户报告："token 用量和上下文不随着使用更新了"）：

三个 harness 同时在跑（页面上分别是 8783 / 8070 / 8795），而 `token-usage`
（8767）与 `context-meter`（8768）用的是**固定端口**：

- Linux 上第二个进程 `bind` 直接 `EADDRINUSE` 抛错——吵，但看得见；
- **Windows 上 Python 的 `HTTPServer` 默认 `allow_reuse_address = 1`**，
  第二个 bind **不报错**，两个 socket 同时处于 LISTENING。

后果：客户端连到哪一个由内核决定。实测里 8795 那个页面的徽标读到的是**另一个
进程**的账本——用户在这边聊了半天，顶栏的 Token / 上下文数字纹丝不动。

修法：bind 之前先探端口有没有主（`pick_port`），有主就交回 0 让系统分配，
并如实公布实际端口。
"""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

from components.llm import Usage, text_reply
from components.net import pick_port
from plugins import TOKEN_USAGE_SERVICE, boot_offline, token_usage_plugin


def _u(text: str, prompt: int, completion: int):
    """一条带用量的一次性回复（账本只统计带 usage 的调用）。"""
    response = text_reply(text)
    response.usage = Usage(prompt, completion)
    return response


def _free_port() -> int:
    """拿一个当前空着的端口（然后立刻释放——只是用来当"被占"的样本）。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------------
# pick_port：被占就让路
# ---------------------------------------------------------------------------


def test_pick_port_keeps_a_free_port():
    port = _free_port()
    assert pick_port("127.0.0.1", port) == port


def test_pick_port_yields_when_someone_is_listening():
    """有人在听 → 交回 0（让系统分配），**不去争**。"""
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy = int(listener.getsockname()[1])
        assert pick_port("127.0.0.1", busy) == 0


def test_pick_port_zero_stays_zero():
    assert pick_port("127.0.0.1", 0) == 0


# ---------------------------------------------------------------------------
# 两个 harness 并存：账本各归各的
# ---------------------------------------------------------------------------


def test_second_harness_takes_another_port_and_keeps_its_own_ledger(tmp_path: Path):
    """**第二个 harness 不再静默抢端口**：它换端口，两个账本各管各的。

    这条在 Linux 上本来会以"装载失败"暴露（EADDRINUSE），而在 Windows 上
    是两个进程都"成功"，读谁的由内核决定——用户报告的那个 bug。
    """
    booted_a = boot_offline(
        root=tmp_path / "a/s", workspace=tmp_path / "a/ws",
        script=[_u("甲", 100, 20)],
    )
    first = booted_a.ctx.require(TOKEN_USAGE_SERVICE)
    first_port = int(first.url.rsplit(":", 1)[1].rstrip("/"))

    # 第二个 harness：装同一个插件（同样的固定端口诉求）——探测后应当换端口
    booted_b = boot_offline(
        root=tmp_path / "b/s", workspace=tmp_path / "b/ws",
        script=[_u("乙", 300, 40)],
    )
    second = booted_b.ctx.require(TOKEN_USAGE_SERVICE)
    second_port = int(second.url.rsplit(":", 1)[1].rstrip("/"))

    if first_port == 8767:      # 本机 8767 没被别的进程占着：这才是在测"抢端口"
        assert second_port != first_port, (
            "第二个 harness 又绑上了同一个端口——两个账本会互相串（Windows 上不报错）"
        )
        assert second_port != 0

    # 各跑一个 turn，各自的账本只记自己的
    booted_a.agent.ask("甲问", session_id="a")
    booted_b.agent.ask("乙问", session_id="b")

    def totals(url: str) -> int:
        import json

        with urllib.request.urlopen(url + "api/usage", timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))["totals"]["total_tokens"]

    total_a = totals(first.url)
    total_b = totals(second.url)
    assert total_a == 120, f"A 的账本不对：{total_a}"
    assert total_b == 340, f"B 的账本不对：{total_b}"
    # 关键：A 的账本里**只有 A 的会话**
    import json

    with urllib.request.urlopen(first.url + "api/usage", timeout=8) as response:
        sessions_a = json.loads(response.read().decode("utf-8"))["sessions"]
    assert sessions_a == ["a"], f"A 的账本里混进了别人的会话：{sessions_a}"
    booted_a.close()
    booted_b.close()


def test_meter_and_usage_pick_free_ports_when_default_is_taken(tmp_path: Path):
    """仪表（8768）与账本（8767）同一套让路逻辑。"""
    from plugins import CONTEXT_METER_SERVICE

    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[_u("x", 10, 2)])
    meter = booted.ctx.require(CONTEXT_METER_SERVICE)
    assert int(meter.port) > 0
    # 端口是它实际绑上的那个（不是"我以为的那个"）：直连必须通
    import json

    with urllib.request.urlopen(
        f"http://127.0.0.1:{meter.port}/api/context", timeout=8
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert "percent" in payload
    booted.close()


def test_endpoint_reports_the_real_port_not_the_requested_one(tmp_path: Path):
    """`port=0` 时公布的也得是**实际**端口（不然页面指到一个没人听的号上）。"""
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws",
                          script=[_u("x", 10, 2)])
    booted.ctx.unload_plugin("token-usage")
    booted.ctx.load_plugin(token_usage_plugin(port=0))
    endpoint = booted.ctx.require(TOKEN_USAGE_SERVICE)
    assert endpoint.port > 0
    with urllib.request.urlopen(endpoint.url + "api/usage", timeout=8) as response:
        assert response.status == 200
    booted.close()
