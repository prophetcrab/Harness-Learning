"""可视化服务的验收测试：全离线，真起一个 stdlib HTTP 服务。

做法：在临时端口起 `ThreadingHTTPServer`，provider 注入 **FakeLLM 剧本**
（与项目其余测试一致——真实 API 只用于手动 demo），用 urllib 打真实 HTTP，验证：

1. 只读接口：/api/config、/api/tools、/api/sessions、/api/sessions/<id>
2. 发送：POST .../send 以 NDJSON 流回事件，且 turn 落盘、可被投影
3. fork：派生新会话，目标已存在则 409
4. 模拟崩溃：往日志尾部写半行 → 端点返回修复报告，事件不丢

注意：本机若配置了系统代理，会把发往 localhost 的请求也拦走（502），
故所有请求都走禁用了代理的 opener。

运行：cd P2_Coding/_07_Plugin_Effect && python -m pytest -q
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.tools import AutoApprove
from harness.webui import server as web

# =========================================================================
# 测试脚手架：起一个真 HTTP 服务（随机端口），provider 用剧本注入
# =========================================================================


@pytest.fixture
def serve(tmp_path):
    """返回 serve(provider_factory) -> base_url；退出时关掉所有起的服务。

    provider 每次 send 请求现场构造一次，所以传进来的工厂每次要返回一个
    带完整"一轮"剧本的 FakeLLM。
    """
    servers = []

    def _serve(provider_factory):
        config = web.ServerConfig(
            root=tmp_path / "sessions",
            workspace=tmp_path / "ws",
            provider_factory=provider_factory,
            provider_label="FakeLLM（测试）",
            approval_factory=AutoApprove,
        )
        httpd = web.create_server(config, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _serve
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def replies(*script):
    """构造一个 provider 工厂：每次调用返回一个带给定剧本的新 FakeLLM。"""
    return lambda: FakeLLM(list(script))


def _opener() -> urllib.request.OpenerDirector:
    """禁掉系统代理的 opener —— 本机代理会把发往 localhost 的请求也拦走（502）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(base: str, path: str):
    with _opener().open(base + path, timeout=10) as response:
        return json.load(response)


def post_raw(base: str, path: str, payload: dict) -> tuple[int, str]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener().open(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post_ndjson(base: str, path: str, payload: dict) -> list[dict]:
    _, body = post_raw(base, path, payload)
    return [json.loads(line) for line in body.splitlines() if line.strip()]


# =========================================================================
# 1) 只读接口
# =========================================================================


def test_config_and_tools(serve):
    base = serve(replies(text_reply("ok")))
    config = get_json(base, "/api/config")
    assert config["provider"].startswith("FakeLLM")
    assert config["max_steps"] >= 1

    tools = get_json(base, "/api/tools")
    names = [t["name"] for t in tools["tools"]]
    assert names[:4] == ["calculate", "read_file", "write_file", "list_files"]
    assert "web_search" not in names  # 默认离线，不注册搜索


def test_index_page_served(serve):
    base = serve(replies(text_reply("ok")))
    with _opener().open(base + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
    assert "日志轨迹" in html
    assert "mini harness" in html


# =========================================================================
# 2) 发送：NDJSON 流 + 落盘 + 投影
# =========================================================================


def test_send_streams_events_and_persists(serve):
    base = serve(
        replies(
            tool_call_reply("calculate", {"expression": "12*3"}),
            text_reply("结果是 36"),
        )
    )
    lines = post_ndjson(base, "/api/sessions/web1/send", {"text": "帮我算 12*3"})

    kinds = [line["kind"] for line in lines]
    assert "turn_start" in kinds
    assert "step_request" in kinds
    assert "step_response" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "__done"

    # 工具确实被调用、且结果是真算出来的
    tool_line = next(line for line in lines if line["kind"] == "tool_result")
    assert tool_line["payload"]["name"] == "calculate"
    assert tool_line["payload"]["result"]["result"] == 36

    done = lines[-1]["payload"]
    assert done["status"] == "done"
    assert "36" in done["final_text"]

    # 落盘 + 投影：GET 会话能看到事件与消息
    session = get_json(base, "/api/sessions/web1")
    types = [e["type"] for e in session["events"]]
    assert "session/start" in types and "tool/result" in types
    roles = [m["role"] for m in session["messages"]]
    assert roles[0] == "system"
    assert "tool" in roles and roles[-1] == "assistant"
    assert session["stats"]["turns"] == 1


def test_send_resumes_existing_session(serve):
    base = serve(replies(text_reply("好的")))
    post_raw(base, "/api/sessions/web2/send", {"text": "你好"})
    lines = post_ndjson(base, "/api/sessions/web2/send", {"text": "再来一次"})
    done = lines[-1]["payload"]
    assert done["turn"] == 2  # 第二个 turn，接续而非从 1 重来


def test_send_requires_text(serve):
    base = serve(replies(text_reply("ok")))
    status, _ = post_raw(base, "/api/sessions/web3/send", {"text": "   "})
    assert status == 400


def test_send_write_file_path(serve):
    base = serve(
        replies(
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hello"}),
            text_reply("已写入"),
        )
    )
    lines = post_ndjson(base, "/api/sessions/web4/send", {"text": "写个文件"})
    tool_line = next(line for line in lines if line["kind"] == "tool_result")
    assert tool_line["payload"]["name"] == "write_file"
    assert "written" in tool_line["payload"]["result"]


# =========================================================================
# 3) 会话列表 / fork
# =========================================================================


def test_sessions_list(serve):
    base = serve(replies(text_reply("好的")))
    post_raw(base, "/api/sessions/lista/send", {"text": "你好"})
    data = get_json(base, "/api/sessions")
    ids = [s["id"] for s in data["sessions"]]
    assert "lista" in ids
    entry = next(s for s in data["sessions"] if s["id"] == "lista")
    assert entry["events"] > 0


def test_fork_and_duplicate_target(serve):
    base = serve(replies(text_reply("好的")))
    post_raw(base, "/api/sessions/src/send", {"text": "你好"})
    status, body = post_raw(base, "/api/sessions/src/fork", {"new_id": "src-fork", "upto": 3})
    assert status == 200
    assert json.loads(body)["id"] == "src-fork"

    # 目标已存在 → 409（fail loud，不覆盖）
    status2, _ = post_raw(base, "/api/sessions/src/fork", {"new_id": "src-fork"})
    assert status2 == 409


# =========================================================================
# 4) 模拟崩溃 → 自动修复
# =========================================================================


def test_simulate_crash_then_repair(serve):
    base = serve(replies(text_reply("好的")))
    post_raw(base, "/api/sessions/crash/send", {"text": "你好"})
    status, body = post_raw(base, "/api/sessions/crash/simulate-crash", {})
    assert status == 200
    payload = json.loads(body)
    # 端点自己返回修复报告（否则会被后续任意一次 load 抢先修掉）
    assert payload["repaired"] is True
    assert payload["dropped_bytes"] > 0

    session = get_json(base, "/api/sessions/crash")
    # 已确认事件不丢：turn/end 仍在
    assert any(e["type"] == "turn/end" for e in session["events"])
    # 已被上一次 load 修复，故此处不再报 repaired
    assert session["repair"]["repaired"] is False
