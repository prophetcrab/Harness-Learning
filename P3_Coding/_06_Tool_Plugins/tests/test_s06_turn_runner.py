"""P3 `_05` 收尾：turn 执行器（并行 / 断线不断 / 中断）测试。

用户提的三件事，根因都在"turn 跑在请求线程里 + 一把全局锁"：

1. **多个会话并行**——原先一把全局锁把整个 turn 包住，A 跑 30 秒 B 就得等；
2. **刷新页面不掉**——原先客户端一断，写帧静默失效，turn 还在跑但再也看不见；
3. **运行中发新消息 → 先问要不要中断**——原先要么排队要么被 `state.sending` 吞掉。

分层验证：先执行器本身（纯线程语义，假 ask 就够），再 agent 服务的锁粒度
与取消，最后 webui 的三个端点（真实 HTTP）。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from components.agent.runner import TurnRunner
from components.llm import LLMRequest, LLMResponse, Message, Usage, text_reply
from plugins import (
    WEBUI_SERVICE,
    boot_offline,
    webui_plugin,
)
from plugins.services import AGENT_SERVICE


def _result(turn=1, status="done", steps=(), final_text=""):
    """一个最小的 TurnResult 替身（执行器只读这几个字段）。"""
    return type(
        "R", (), {"turn": turn, "status": status, "steps": list(steps), "final_text": final_text}
    )()


# ---------------------------------------------------------------------------
# 执行器本身（纯线程 + 订阅语义，不起服务）
# ---------------------------------------------------------------------------


def test_different_sessions_run_in_parallel():
    """**核心**：两条会话的 turn 同时在跑（不是排队）。

    判据不止"都快"：用闸门证明它们**真的重叠**——A 卡在闸门上时，B 必须能
    跑完（若串行，B 会排在 A 后面一起等闸门，永远不完成）。
    """
    gate = threading.Event()
    started: list[str] = []
    lock = threading.Lock()

    def ask(session_id, text, sink=None):
        with lock:
            started.append(session_id)
        if session_id == "a":
            gate.wait(timeout=5)          # A 卡住，直到我们放行
        return _result(final_text=text)

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    run_a = runner.start("a", "甲")
    assert run_a is not None
    for _ in range(300):                  # 等 A 真的进了 ask
        with lock:
            if "a" in started:
                break
        time.sleep(0.01)

    run_b = runner.start("b", "乙")
    assert run_b is not None
    assert run_b.done.wait(timeout=5), "B 被 A 挡住了——还是串行"
    assert not run_a.done.is_set(), "A 应该还被闸门卡着（证明两者真的重叠）"
    gate.set()
    assert run_a.done.wait(timeout=5)
    assert sorted(started) == ["a", "b"]


def test_same_session_is_rejected_while_running():
    """同一条会话同时只允许一个 turn（历史是那一份对象，交叉写必然乱）。"""
    gate = threading.Event()

    def ask(session_id, text, sink=None):
        gate.wait(timeout=5)
        return _result()

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    assert runner.start("s", "第一条") is not None
    for _ in range(300):
        if runner.get("s") is not None:
            break
        time.sleep(0.01)
    assert runner.start("s", "第二条") is None, "同会话第二次应被拒（调用方回 409）"
    gate.set()
    for _ in range(300):                  # 等第一个跑完
        if runner.get("s") is None:
            break
        time.sleep(0.01)
    assert runner.start("s", "第三条") is not None, "前一个跑完后应能再起"


def test_frames_reach_all_subscribers_and_survive_one_leaving():
    """帧广播给**所有**订阅者；一个订阅者退订不影响其他人。

    这就是"刷新页面不掉"的机制：新连接订阅上、旧连接退订，turn 一直在跑。
    """
    gate = threading.Event()

    def ask(session_id, text, sink=None):
        gate.wait(timeout=5)
        return _result()

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    run = runner.start("s", "嗨")
    seen_a: list[str] = []
    seen_b: list[str] = []

    def sink_a(kind, payload):
        seen_a.append(kind)

    def sink_b(kind, payload):
        seen_b.append(kind)

    assert run.subscribe(sink_a) is True
    run.push("step_request", {"step": 1})
    assert run.subscribe(sink_b) is True          # 后到的只收后续帧
    run.push("step_delta", {"text": "你"})
    run.unsubscribe(sink_a)                        # 旧连接走了
    run.push("step_delta", {"text": "好"})
    gate.set()
    assert run.done.wait(timeout=5)
    # 终帧也是广播出来的（等终帧的订阅者靠它收尾）：A 退得早，B 收到它
    assert "__done" in seen_b, "订阅者应收到终帧"
    assert seen_a[:2] == ["step_request", "step_delta"]
    assert "__done" not in seen_a, "A 早退了，终帧不该再推给它"
    assert [k for k in seen_b if k != "__done"] == ["step_delta", "step_delta"]
    assert run.final is not None and run.final[0] == "__done"


def test_subscribe_after_finish_says_so_and_final_is_kept():
    """跑完之后才订阅 → False，且终帧留着（晚到者能立刻收尾，不干等）。

    "刷新刚好撞上收尾"是常见情形：不保留终帧的话，新页面会挂在等下一帧上。
    """

    def ask(session_id, text, sink=None):
        return _result(turn=3, steps=[1, 2], final_text="好")

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    run = runner.start("s", "嗨")
    assert run.done.wait(timeout=5)
    assert run.subscribe(lambda k, p: None) is False
    assert run.final == ("__done", {"turn": 3, "status": "done", "steps": 2, "final_text": "好"})


def test_failing_turn_becomes_error_frame_not_an_exception():
    """turn 抛异常也要以 `__error` 帧收尾（后台线程里抛出没人接得住）。"""

    def ask(session_id, text, sink=None):
        raise RuntimeError("模型炸了")

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    run = runner.start("s", "嗨")
    assert run.done.wait(timeout=5)
    assert run.final is not None
    kind, payload = run.final
    assert kind == "__error" and "模型炸了" in payload["message"]


def test_unsubscribed_or_broken_sink_does_not_stop_the_turn():
    """订阅者写失败（客户端断了）不打断 turn——**这正是"没人看也照跑完"**。"""
    gate = threading.Event()

    def ask(session_id, text, sink=None):
        gate.wait(timeout=5)
        return _result()

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    run = runner.start("s", "嗨")

    def broken(kind, payload):
        raise ConnectionResetError("客户端跑了")

    run.subscribe(broken)
    run.push("step_request", {"step": 1})        # 不该抛出来
    gate.set()
    assert run.done.wait(timeout=5), "坏订阅者不该让 turn 卡住"


def test_cancel_forwards_and_reports_whether_anything_ran():
    """中断转给 cancel 回调；没有在跑的 turn → False（调用方回 404）。"""
    calls: list[str] = []
    gate = threading.Event()

    def ask(session_id, text, sink=None):
        gate.wait(timeout=5)
        return _result(status="cancelled")

    runner = TurnRunner(ask=ask, cancel=lambda sid: (calls.append(sid), True)[1])
    assert runner.cancel("nobody") is False        # 没在跑
    run = runner.start("s", "嗨")
    for _ in range(300):
        if runner.get("s") is not None:
            break
        time.sleep(0.01)
    assert runner.cancel("s") is True
    assert calls == ["s"]
    gate.set()
    assert run.done.wait(timeout=5)


def test_ask_receives_this_runs_sink_and_frames_go_through_it():
    """执行器把**本 run 的写帧端**交给 ask，调用方自己决定挂到哪儿。

    为什么不用"当前 turn"的全局去找：本项目的阶段目录各持一份同名包、测试会
    驱逐 `sys.modules`——模块级全局（如 ContextVar）在那种情形下"set 的那份
    与读的那份不是同一个"，帧会静默丢失（实测踩到，见 runner.py 的模块
    docstring）。把 sink 显式交给调用方，就不依赖任何全局。
    """
    received: list[tuple[str, dict]] = []
    seen: list[object] = []

    def ask(session_id, text, sink):
        seen.append(sink)
        sink("custom_event", {"n": 1})            # 调用方在 turn 里就能写帧
        return _result()

    runner = TurnRunner(ask=ask, cancel=lambda sid: False)
    run = runner.start("s", "嗨", sink=lambda k, p: received.append((k, p)))
    assert run.done.wait(timeout=5)
    assert len(seen) == 1 and callable(seen[0])
    assert received[0] == ("custom_event", {"n": 1})
    assert received[-1][0] == "__done"           # 终帧也走同一条路


# ---------------------------------------------------------------------------
# agent 服务：锁粒度 / 取消 / 追加订阅
# ---------------------------------------------------------------------------


class GatedLLM:
    """一个会"卡住"的 provider：**第一次**回复前等闸门（模拟慢模型）。

    只卡第一次——否则第二条会话也会卡住，"并行"这件事就验不出来了。
    `entered` 事件在开始等待前 set，测试用它确定"已经进到模型调用"。
    用它才能证明并行/中断：剧本 provider 是瞬时的，来不及观察。
    """

    def __init__(self, gate: threading.Event, *, reply: str = "好") -> None:
        self._gate = gate
        self._reply = reply
        self._calls = 0
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.requests.append(request)
            first = self._calls == 0
            self._calls += 1
        if first:
            self.entered.set()
            self._gate.wait(timeout=5)
        return LLMResponse(
            message=Message(role="assistant", content=self._reply), usage=Usage()
        )


def _boot_with(llm, tmp_path: Path):
    """用给定 provider 装一套 P3（只换 llm 插件，其余走 boot_offline 的组合）。"""
    from plugins import AgentPlugin, boot, memory_fs_plugin, scripted_proc_plugin, tool_plugins
    from plugins.llm import LlmPlugin
    from plugins.prompt import PromptPlugin
    from plugins.sessions import SessionsPlugin
    from plugins.tools import ToolsPlugin

    return boot(
        plugins=[
            LlmPlugin(llm),
            memory_fs_plugin(),
            scripted_proc_plugin(),
            SessionsPlugin(tmp_path / "s"),
            PromptPlugin(workspace=tmp_path / "ws"),
            ToolsPlugin(),
            *tool_plugins(workspace=tmp_path / "ws"),
            AgentPlugin(),
        ],
        workspace=tmp_path / "ws",
    )


def test_agent_runs_two_sessions_concurrently(tmp_path: Path):
    """**agent 服务的锁是每条会话一把**：A 卡在模型调用上时 B 能跑完。

    全局锁下这条必失败（B 会排在 A 后面，而 A 正卡在闸门上）。
    """
    gate = threading.Event()
    llm = GatedLLM(gate)
    booted = _boot_with(llm, tmp_path)
    agent = booted.ctx.require(AGENT_SERVICE)
    done: dict[str, object] = {}

    def run_a():
        done["a"] = agent.ask("甲", session_id="a")

    thread = threading.Thread(target=run_a, daemon=True)
    thread.start()
    assert llm.entered.wait(timeout=5), "A 没进到模型调用"   # A 此刻持有会话锁并卡住

    # B 若被全局锁挡住，这里会一直等到闸门超时（5 秒）才返回——用耗时判据抓它
    started = time.time()
    result_b = agent.ask("乙", session_id="b")
    elapsed = time.time() - started
    assert result_b.status == "done"
    assert elapsed < 2, f"B 等了 {elapsed:.1f} 秒——还是被 A 全局挡住（没并行）"

    gate.set()
    thread.join(timeout=6)
    assert done.get("a") is not None
    booted.close()


def test_agent_cancel_reports_missing_session(tmp_path: Path):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=[text_reply("好")])
    assert booted.ctx.require(AGENT_SERVICE).cancel("从没打开过") is False
    booted.close()


def test_cancel_interrupts_a_turn_stuck_in_the_model_call(tmp_path: Path):
    """**中断在"模型正在想"时也生效**（回归：取消只在 step 边界检查过）。

    非流式 provider 的调用要整个返回才轮得到边界检查——原先在这段时间里
    按下中断会被忽略，turn 照样以 `done` 收尾，用户以为没生效。
    现在模型返回后补了一刀：`cancelled` 收尾 + 工具调用不执行。
    """
    gate = threading.Event()
    booted = _boot_with(GatedLLM(gate, reply="半截回答"), tmp_path)
    agent = booted.ctx.require(AGENT_SERVICE)
    done: dict[str, object] = {}

    def run():
        done["r"] = agent.ask("嗨", session_id="s")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.15)                      # 让它进到模型调用
    assert agent.cancel("s") is True
    gate.set()                            # 模型"想完了"返回
    thread.join(timeout=6)
    result = done.get("r")
    assert result is not None
    assert result.status == "cancelled", f"应 cancelled 收尾，实际 {result.status}"
    booted.close()


def test_cancel_does_not_poison_later_turns(tmp_path: Path):
    """**中断过一次，不会让这条会话以后每句话都被取消**（真 bug）。

    回归背景：`AgentLoop.cancel()` 原先写成**粘性**的（"之后所有 turn 都取消"）。
    在"中断当前 turn 再重新发一条"的用法下，那条重发的消息会在 0 步处立刻
    被取消——界面上看是"这条会话坏了"（实测：重发那条显示 `cancelled · 0 步`）。
    现在取消标记在每个 turn 开始时清零。
    """
    gate = threading.Event()
    booted = _boot_with(GatedLLM(gate), tmp_path)
    agent = booted.ctx.require(AGENT_SERVICE)
    first: dict[str, object] = {}

    def run_first():
        first["r"] = agent.ask("第一条", session_id="s")

    thread = threading.Thread(target=run_first, daemon=True)
    thread.start()
    time.sleep(0.15)
    assert agent.cancel("s") is True       # 中断第一条
    gate.set()
    thread.join(timeout=6)
    assert first["r"].status == "cancelled"

    # 第二条：**必须正常跑完**（不该被上一次的取消残留影响）
    second = agent.ask("第二条", session_id="s")
    assert second.status == "done", f"中断后的下一条应正常完成，实际 {second.status}"
    assert second.final_text == "好"
    booted.close()


def test_agent_watch_appends_without_replacing(tmp_path: Path):
    """`watch` 是追加订阅（刷新时新旧连接并存）；`open(on_event=)` 仍是替换语义。"""
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[text_reply("好"), text_reply("再见")],
    )
    agent = booted.ctx.require(AGENT_SERVICE)
    got_a: list[str] = []
    got_b: list[str] = []
    unwatch_a = agent.watch("s", lambda kind, payload: got_a.append(kind))
    agent.watch("s", lambda kind, payload: got_b.append(kind))
    agent.ask("嗨", session_id="s")
    assert got_a and got_b, "两个订阅者都该收到事件"

    unwatch_a()
    before = len(got_a)
    agent.ask("再说", session_id="s")
    assert len(got_a) == before, "退订后不该再收"
    assert len(got_b) > before, "另一个订阅者照常收"
    booted.close()


# ---------------------------------------------------------------------------
# webui 的三个端点（真实 HTTP）
# ---------------------------------------------------------------------------


def _boot_web(tmp_path: Path, script):
    booted = boot_offline(root=tmp_path / "s", workspace=tmp_path / "ws", script=script)
    booted.ctx.load_plugin(webui_plugin(port=0))
    return booted, booted.ctx.require(WEBUI_SERVICE).url


def _get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    """POST 一个**返回 JSON** 的端点（如 /interrupt）。

    `/send` 与 `/turn` 返回的是 NDJSON 流，不能用这个——见 `_read_stream`。
    """
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _post_stream(url: str, payload: dict, timeout: float = 15) -> tuple[int, list[dict]]:
    """POST 一个**返回 NDJSON** 的端点，读完所有帧。

    流式响应不能用 `json.loads(整个 body)`——多行 JSON 会报 "Extra data"。

    **连接被中止但已经收到帧**时返回已收到的那些（Windows 上偶发
    `ConnectionAbortedError`：服务端收尾与客户端读取撞在一起）。真正的断言
    都落在"收到过哪些帧"上，所以这不是放水；完全收不到帧才视为失败。
    """
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    body = ""
    status = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8")
    except (ConnectionAbortedError, ConnectionResetError) as exc:
        frames_now = _parse_frames(body)
        if not frames_now:                       # 一帧都没收到：是真问题
            raise AssertionError(f"流被中止且没收到任何帧：{exc}") from exc
    frames = _parse_frames(body)
    return status, frames


def _parse_frames(body: str) -> list[dict]:
    frames: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return frames


def test_abandoning_a_stream_does_not_poison_the_next_request(tmp_path: Path):
    """**中途放弃一条流，不能让下一次请求拿到垃圾响应**（真故障，症状很隐晦）。

    回归背景：客户端随时可能放弃读取（刷新页面、切会话、中断）。原先流式响应
    与普通请求**共用同一条 keep-alive 连接**——服务端还会继续往这条连接里写
    剩下的帧，那些字节残留下来，下一次复用连接时就解析歪了。实测：中断后
    紧接着的 `GET /api/sessions` 收到 **501**，界面上的表现是"重发无声无息地
    没发生"（没有任何报错，因为那次 fetch 本身也没被 await 检查）。

    修法：流式响应一律 `Connection: close`（写完即关）——会被中途放弃的响应
    不适合复用连接。
    """
    gate = threading.Event()
    llm = GatedLLM(gate)
    booted = _boot_with(llm, tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    # 起一个 turn，只读一帧就断（模拟刷新）
    conn = http.client.HTTPConnection("127.0.0.1", int(url.split(":")[2].split("/")[0]), timeout=15)
    conn.request("POST", "/api/sessions/poison/send",
                 body=json.dumps({"text": "嗨"}), headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200
    # 声明了 close：服务端不会复用这条连接（这就是修复的判据）
    assert (resp.getheader("Connection") or "").lower() == "close", \
        "流式响应必须声明 Connection: close，否则被放弃的流会污染后续请求"
    resp.read(1)                      # 读一点点
    conn.close()                      # 然后像刷新页面那样直接走人
    gate.set()

    # 后续请求必须干净（原先这里会拿到 501）
    status, body = _get_json(url + "api/sessions")
    assert status == 200, f"放弃流之后的下一次请求应正常，实际 {status}"
    assert "sessions" in body
    booted.close()


def test_turn_endpoint_404_when_nothing_running(tmp_path: Path):
    """没有在跑的 turn：GET /turn → 404（前端静默跳过，不当错误处理）。"""
    booted, url = _boot_web(tmp_path, [text_reply("好")])
    status, body = _get_json(url + "api/sessions/idle/turn")
    assert status == 404 and body.get("running") is False
    booted.close()


def test_interrupt_endpoint_404_when_nothing_running(tmp_path: Path):
    booted, url = _boot_web(tmp_path, [text_reply("好")])
    status, _ = _post_json(url + "api/sessions/idle/interrupt", {})
    assert status == 404
    booted.close()


def test_send_rejects_a_second_turn_with_409(tmp_path: Path):
    """同会话在跑时再 POST /send → **409 + busy 标记**。

    不排队（排队会让用户以为发出去了）；前端拿到 409 弹"中断 / 等它跑完"。
    用真正的慢模型占住会话，第二条才真的撞上"正在跑"。
    """
    gate = threading.Event()
    llm = GatedLLM(gate)
    booted = _boot_with(llm, tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    first: dict = {}

    def send_first() -> None:
        status, frames = _post_stream(url + "api/sessions/s1/send", {"text": "第一条"})
        first["status"], first["frames"] = status, frames

    thread = threading.Thread(target=send_first, daemon=True)
    thread.start()
    assert llm.entered.wait(timeout=5), "第一条没跑起来"
    status, body = _post_json(url + "api/sessions/s1/send", {"text": "第二条"})
    assert status == 409, f"应回 409，实际 {status} {body}"
    assert body.get("busy") is True and "中断" in body.get("error", "")

    gate.set()
    thread.join(timeout=10)
    booted.close()


def test_sessions_list_marks_running(tmp_path: Path):
    """会话列表带 `running` 标记——页面靠它在列表里标"运行中"并允许中断。"""
    gate = threading.Event()
    llm = GatedLLM(gate)
    booted = _boot_with(llm, tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    thread = threading.Thread(
        target=lambda: _post_stream(
            url + "api/sessions/" + urllib.parse.quote("跑着的") + "/send", {"text": "嗨"}
        ),
        daemon=True,
    )
    thread.start()
    assert llm.entered.wait(timeout=5), "turn 没跑起来"
    _, body = _get_json(url + "api/sessions")
    item = next((s for s in body["sessions"] if s["id"] == "跑着的"), None)
    assert item is not None, "正在跑的会话应出现在列表里"
    assert item["running"] is True
    gate.set()
    thread.join(timeout=10)
    booted.close()


def test_interrupt_stops_the_running_turn_and_writes_cancelled(tmp_path: Path):
    """**中断真的停住 turn**：HTTP 请求 → 主循环以 cancelled 收尾并推给订阅者。

    用一个卡在模型调用上的 provider，确保打断发生在"模型正在想"的时候
    （这正是原先被忽略的情形）。
    """
    gate = threading.Event()
    llm = GatedLLM(gate, reply="半截")
    booted = _boot_with(llm, tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    outcome: dict = {}

    def subscribe():
        outcome["result"] = _post_stream(url + "api/sessions/s1/send", {"text": "嗨"})

    thread = threading.Thread(target=subscribe, daemon=True)
    thread.start()
    assert llm.entered.wait(timeout=5), "turn 没跑起来"
    status, body = _post_json(url + "api/sessions/s1/interrupt", {})
    assert status == 200 and body.get("ok") is True, f"{status} {body}"
    gate.set()                             # 模型"想完了"，循环这时才看到取消标记
    thread.join(timeout=10)

    assert "result" in outcome, "订阅线程没返回"
    _, frames = outcome["result"]
    kinds = [frame["kind"] for frame in frames]
    assert "__done" in kinds, f"订阅方应收到终帧，实际 {kinds}"
    terminal = next(f for f in frames if f["kind"] == "__done")
    assert terminal["payload"]["status"] == "cancelled", terminal
    booted.close()


def test_second_turn_stream_starts_at_its_own_turn_start(tmp_path: Path):
    """**回归：排队的第二个 turn 不该收到前任的帧**（真 bug）。

    背景：推流器原先在**拿会话锁之前**就挂上。于是当第二个 turn 在门外排队时，
    它已经"在听"了——上一个 turn 的收尾帧（`step_response` / `turn_end` / …）会
    先流到它这里，然后才是自己的 `turn_start`。客户端照着渲染，表现为凭空多出
    一条 AI 气泡和一个"turn 完成"行。

    修法：`AgentService.ask` 接受 `on_started` 回调，在**拿到会话锁之后、turn 开始
    之前**调用——界面在那里才挂推流器。

    构造方式：停用再启用 webui 插件（执行器是新的、空表），于是同会话能再起一个
    turn，与仍在跑的第一个并行——正是"排队"的真实触发条件。
    """
    gate = threading.Event()

    class GatedFirst:
        """只卡第一个调用，第二个放行。"""

        def __init__(self) -> None:
            self.calls = 0
            self.entered = threading.Event()
            self._lock = threading.Lock()

        def complete(self, request):
            with self._lock:
                self.calls += 1
                n = self.calls
            self.entered.set()
            if n == 1:
                gate.wait(timeout=10)
            return LLMResponse(
                message=Message(role="assistant", content=f"回复{n}"), usage=Usage()
            )

    llm = GatedFirst()
    booted = _boot_with(llm, tmp_path)
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    streams: dict[str, list[str]] = {}

    def send(key: str, text: str, base: str) -> None:
        try:
            status, frames = _post_stream(base + "api/sessions/same/send", {"text": text})
            streams[key] = [f["kind"] for f in frames]
        except Exception as exc:                     # noqa: BLE001 —— 记下来供断言
            streams[key] = [f"错误：{exc}"]

    first = threading.Thread(target=send, args=("one", "甲", url), daemon=True)
    first.start()
    assert llm.entered.wait(timeout=10), "第一个 turn 没跑起来"

    # 换一个执行器（模拟 webui 重装）：它不知道第一个 turn 还在跑，于是能再起一个
    booted.ctx.disable_plugin("webui")
    booted.ctx.enable_plugin("webui")
    url2 = booted.ctx.require(WEBUI_SERVICE).url

    second = threading.Thread(target=send, args=("two", "乙", url2), daemon=True)
    second.start()
    time.sleep(0.8)                                  # 让第二个挂上（此时在等会话锁）
    gate.set()
    first.join(timeout=20)
    second.join(timeout=20)

    two = streams.get("two", [])
    assert two, "第二个 turn 没有流回来"
    assert two[0] == "turn_start", (
        f"第二个 turn 的第一帧应该是它自己的 turn_start，实际 {two[:4]}"
        "（说明它收到了前任的收尾帧）"
    )
    booted.close()
