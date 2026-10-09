"""_11 顶层 webui 包（P2 形态页面）的接线测试：全离线，真起 HTTP 服务。

与基线 webui 测试（`test_session_follow_webui.py`，22 基线用例的一部分）的关系：
那份验证"M1–M3 形态"的页面（`python -m harness.webui.server`）仍可用；本文件
验证**升级版**（`webui/server.py`）——配置驱动 + 全工具面 + 插件台账。

做法与基线测试一致：临时端口起 `ThreadingHTTPServer`；dev profile 全离线
（FakeLLM + MemoryFS + 剧本命令），因此可以做确定性断言。所有请求走禁代理 opener
（本机若配了系统代理会把 localhost 请求也拦走）。

覆盖：
1. /api/config：profile、插件台账、每行的来源层、工作区；
2. /api/ctx：effect 台账 / 槽位 / 全工具面（8 件）；
3. /api/tools：工具说明书含 _11 新工具；
4. POST send：NDJSON 流跑完一个 turn；工具结果落日志、可投影；
5. 页面本身：返回的是顶层 webui 的 index.html（含 profile/插件徽章元素）；
6. --deny-writes：需审批工具被拒（拒绝路径可达）。

运行：cd P2_Coding/mini_harness && python -m pytest -q
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from webui import server as p2web

STAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILES = STAGE_ROOT / "profiles"


# ---------------------------------------------------------------------------
# 测试脚手架：起真 HTTP 服务（随机端口，dev profile）
# ---------------------------------------------------------------------------


class _Config:
    """构造 WebConfig（跳过 argparse）：dev profile + 给定 root。

    注意：工作区不再由命令行指定——它来自配置树的 toolbox 行（`--workspace`
    不是本页面的参数；这正是"配置驱动"的体现）。测试需要隔离时改 root，
    工作区用 profile 默认（demo_workspace/ws，MemoryFS 下无磁盘副作用）。
    """

    def __new__(cls, tmp_path: Path, *, deny_writes: bool = False):
        args = p2web.build_parser().parse_args(
            ["--profile", "dev", "--root", str(tmp_path / "sessions")]
        )
        config = p2web.build_config(args)
        if deny_writes:
            from harness.tools.approval import AutoDeny

            config.approval_factory = lambda: AutoDeny("测试：禁止写操作")
        return config


@pytest.fixture
def serve(tmp_path):
    servers = []

    def _start(*, deny_writes: bool = False) -> tuple[str, Path]:
        config = _Config(tmp_path, deny_writes=deny_writes)
        httpd = p2web.create_server(config, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        host, port = httpd.server_address[:2]
        return f"http://{host}:{port}", config.workspace

    yield _start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _opener() -> urllib.request.OpenerDirector:
    """禁代理 opener（系统代理会把 localhost 也拦走）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(base: str, path: str) -> dict:
    with _opener().open(base + path, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _send(base: str, session: str, text: str) -> list[dict]:
    """POST send，把 NDJSON 流收集为事件列表。"""
    body = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/sessions/{session}/send",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _opener().open(request, timeout=60) as response:
        lines = response.read().decode("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# =========================================================================
# 1) 配置与台账接口
# =========================================================================


def test_config_exposes_profile_and_row_sources(serve):
    base, _ = serve()
    cfg = _get(base, "/api/config")
    assert cfg["profile"] == "dev"
    assert cfg["provider"].startswith("llm:fake")
    sources = {row["id"]: row["source"] for row in cfg["rows"]}
    assert sources == {"llm": "dev.yaml", "fs": "dev.yaml",
                       "subprocess": "dev.yaml", "toolbox": "base.yaml"}


def test_ctx_exposes_effect_ledger_and_full_toolbox(serve):
    base, _ = serve()
    ctx = _get(base, "/api/ctx")
    assert ctx["effects"] == [
        "plugin:llm:FakeLLM",
        "plugin:fs:MemoryFS",
        "plugin:subprocess:ScriptedSubprocess",
        "plugin:toolbox",
    ]
    assert ctx["slots"] == ["llm", "fs", "subprocess", "toolbox"]
    assert ctx["tools"] == [
        "calculate", "read_file", "write_file", "list_files",
        "edit_file", "search_text", "find_files", "shell",
    ]


def test_tools_include_coding_tools(serve):
    base, _ = serve()
    tools = _get(base, "/api/tools")["tools"]
    names = [t["name"] for t in tools]
    assert {"edit_file", "search_text", "find_files"} <= set(names)
    edit = next(t for t in tools if t["name"] == "edit_file")
    assert "恰好出现一次" in edit["description"]  # 语义写明在说明书里


# =========================================================================
# 2) 发送：NDJSON 流 + 落盘
# =========================================================================


def test_send_streams_turn_and_persists(serve):
    base, _ = serve()
    events = _send(base, "s-webui", "帮我算 6*7")
    kinds = [e["kind"] for e in events]
    assert "turn_start" in kinds and "tool_result" in kinds and "turn_end" in kinds
    done = events[-1]
    assert done["kind"] == "__done" and done["payload"]["status"] == "done"

    # 会话详情：事件已落盘、可投影
    detail = _get(base, "/api/sessions/s-webui")
    types = [e["type"] for e in detail["events"]]
    assert "session/start" in types and "tool/result" in types


def test_send_full_toolbox_visible_in_session(serve):
    """新装配路径的痕迹：开场渲染的**装配单**（_03）随 session/start 落日志。

    注意不要求出现 system/message——它只在"渲染结果与上一次不同"时记录
    （`_02` 的变化才记录语义；同一秒内两次渲染相同就不产生）。
    """
    base, _ = serve()
    _send(base, "s-trace", "你好")
    detail = _get(base, "/api/sessions/s-trace")
    starts = [e for e in detail["events"] if e["type"] == "session/start"]
    assert starts and "prompt_trace" in starts[0]["data"], "开场装配单应随 session/start 落日志"
    trace = starts[0]["data"]["prompt_trace"]
    assert [s["name"] for s in trace["sections"]] == ["role", "tools", "env", "style"]
    assert trace["variables"]["platform"]  # 渲染时采集的运行时上下文（_02）


def test_deny_writes_blocks_tool(serve):
    base, _ = serve(deny_writes=True)
    events = _send(base, "s-deny", "帮我算 6*7")  # 剧本第一幕是 calculate，不需审批
    assert events[-1]["kind"] == "__done"

    # 第二幕写文件：--deny-writes 下应被拒（拒绝也回给模型、循环照常收尾）
    events = _send(base, "s-deny", "写个文件")
    detail = _get(base, "/api/sessions/s-deny")
    denied = [
        e for e in detail["events"]
        if e["type"] == "tool/result" and e["data"].get("is_error")
    ]
    assert denied and "禁止" in denied[0]["data"]["result"]["error"]


# =========================================================================
# 3) 页面本身
# =========================================================================


def test_page_is_p2_index(serve):
    base, _ = serve()
    with _opener().open(base + "/", timeout=30) as response:
        html = response.read().decode("utf-8")
    assert "profileBadge" in html  # 新加的 profile 徽章
    assert "pluginList" in html    # 插件台账展示位
    assert "system/message" in html  # 新事件类型的标签
