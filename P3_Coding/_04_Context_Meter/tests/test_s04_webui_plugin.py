"""P3 界面插件测试：装载即起服务、卸载即停服、只读 API、流式对话。

全部走真实 HTTP（起在 127.0.0.1 的随机端口上，port=0 由系统分配）——
"界面是插件"的验收就是"装载能在浏览器里用、卸载不留痕迹"。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from plugins import (
    WEBUI_SERVICE,
    boot_offline,
    local_fs_plugin,
    webui_plugin,
)


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.load(response)


def _get_text(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def _post_ndjson(url: str, payload: dict) -> list[dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    frames: list[dict] = []
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.headers["Content-Type"].startswith("application/x-ndjson")
        for line in response.read().decode("utf-8").splitlines():
            if line.strip():
                frames.append(json.loads(line))
    return frames


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _boot(tmp_path: Path, script: list):
    return boot_offline(root=tmp_path / "sessions", workspace=tmp_path / "ws", script=script)


# ---------------------------------------------------------------------------
# 生命周期：装载即起、卸载即停
# ---------------------------------------------------------------------------


def test_load_starts_server_and_unload_releases_port(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    service = booted.ctx.require(WEBUI_SERVICE)
    assert service.port > 0
    assert _port_open(service.port)

    booted.ctx.unload_plugin("webui")
    assert not _port_open(service.port)  # 停服 + 端口释放
    assert "webui" not in booted.ctx.services
    booted.close()


def test_webui_missing_dependency_keeps_plugin_pending(tmp_path: Path):
    """缺 agent 服务时界面插件不应激活（fail loud 的装载语义）。"""
    from kernel import Context

    ctx = Context()
    ctx.load_plugin(webui_plugin(port=0))
    assert ctx.pending_plugins == ["webui"]
    assert ctx.missing_dependencies("webui") == ["agent", "sessions"]


def test_booted_close_also_stops_webui(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    port = booted.ctx.require(WEBUI_SERVICE).port
    booted.close()  # 整体卸载：界面插件同样被回卷（逆序卸载）
    assert not _port_open(port)


# ---------------------------------------------------------------------------
# 只读 API：台账 / 会话 / 事件
# ---------------------------------------------------------------------------


def test_ledger_reports_plugins_services_and_tools(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    status, ledger = _get(url + "api/ledger")
    assert status == 200
    names = [entry["name"] for entry in ledger["plugins"]]
    assert "agent" in names and "webui" in names
    assert ledger["services"] == [
        "llm",
        "fs",
        "subprocess",
        "sessions",
        "prompt",
        "tools",
        "token-usage",          # `_03`
        "context-meter",        # `_04`
        "agent",
        "webui",
    ]
    assert "calculate" in ledger["tools"]
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_two_consecutive_turns_on_same_session(tmp_path: Path):
    """同一会话连发两条，第二轮的 turn 必须能跑完。

    曾经的回归（"只聊了一轮就死"）：AgentLoop 的 sink 在**首次打开会话时**
    捕获了观察者（第一轮请求的推流器，绑定在第一个 HTTP 连接上）。第一轮结束
    后连接被关；第二轮起，事件在记录完 turn/start + user/message 后就被推给
    那个**已关闭的旧套接字**，写死套接字抛 OSError，把整轮打断——于是每一轮
    都停在同一个位置。这个用例用真实 HTTP 连发两轮把它钉住。
    """
    booted = _boot(tmp_path, [text_reply("第一轮回答"), text_reply("第二轮回答")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    first = _post_ndjson(url + "api/sessions/same/send", {"text": "第一问"})
    assert first[-1]["kind"] == "__done"
    assert first[-1]["payload"]["turn"] == 1

    second = _post_ndjson(url + "api/sessions/same/send", {"text": "第二问"})
    assert second[-1]["kind"] == "__done", f"第二轮应跑完，实际：{second[-1]}"
    assert second[-1]["payload"]["turn"] == 2
    assert second[-1]["payload"]["final_text"] == "第二轮回答"
    # 第二轮的事件也要**实时推给当轮的那个连接**（修前：被推给第一轮的旧连接）
    kinds = [frame["kind"] for frame in second]
    assert "turn_start" in kinds and "step_response" in kinds and "turn_end" in kinds

    # 两条 turn/end 都在日志里（没有半截 turn）
    _, detail = _get(url + "api/sessions/same/events")
    types = [event["type"] for event in detail["events"]]
    assert types.count("turn/start") == 2 and types.count("turn/end") == 2

    booted.ctx.unload_plugin("webui")
    booted.close()


def test_sessions_and_event_api_reflect_turns(tmp_path: Path):
    booted = _boot(
        tmp_path,
        [
            tool_call_reply("calculate", {"expression": "2+3"}),
            text_reply("2+3=5。"),
        ],
    )
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    frames = _post_ndjson(url + "api/sessions/s1/send", {"text": "帮我算 2+3"})
    done = frames[-1]
    assert done["kind"] == "__done"
    assert done["payload"]["status"] == "done"
    assert any(frame["kind"] == "tool_result" for frame in frames)  # 事件流实时推

    _, overview = _get(url + "api/sessions")
    assert overview["sessions"][0]["id"] == "s1"
    assert overview["sessions"][0]["turns"] == 1

    _, detail = _get(url + "api/sessions/s1/events")
    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert [event["type"] for event in detail["events"]][0] == "session/start"
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_index_page_served_and_404_for_unknown_route(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    status, html = _get_text(url)
    assert status == 200 and "<html" in html.lower() and "插件台账" in html

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(url + "api/nope")
    assert excinfo.value.code == 404
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_send_requires_text(tmp_path: Path):
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    request = urllib.request.Request(
        url + "api/sessions/s1/send",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 400
    booted.ctx.unload_plugin("webui")
    booted.close()


# ---------------------------------------------------------------------------
# 与其它插件协作：界面跟随 fs 组合变化（新会话用新组合）
# ---------------------------------------------------------------------------


def test_web_send_uses_current_plugin_combination(tmp_path: Path):
    from components.tools import AutoApprove

    booted = boot_offline(
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=AutoApprove(),  # 写文件需审批：本用例放行（默认是 fail-closed 拒绝）
        script=[
            tool_call_reply("write_file", {"path": "a.txt", "content": "hi"}),
            text_reply("已写入"),
        ],
    )

    # 换掉 fs（MemoryFS → LocalFS）：tools/agent 自动重建。
    # 注意：agent 重建会让**依赖 agent 的界面插件一并退位再激活**
    # （依赖链 webui → agent → fs 的级联），所以这一步放在装载界面之前做——
    # 若界面已装载，它会跟着重启（固定端口时重新绑定同一端口）。
    booted.ctx.unload_plugin("fs:MemoryFS")
    booted.ctx.load_plugin(local_fs_plugin(tmp_path / "disk_ws"))

    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    frames = _post_ndjson(url + "api/sessions/s2/send", {"text": "写 a.txt"})
    assert frames[-1]["payload"]["status"] == "done"
    assert (tmp_path / "disk_ws" / "a.txt").read_text(encoding="utf-8") == "hi"
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_webui_restarts_when_upstream_plugin_swapped(tmp_path: Path):
    """级联语义的显式验收：换 fs → 界面插件被迫退位/重启，端口随之更换。

    这是"严格依赖 + 依赖者先退位"的直接后果——记录在案（README 的边界一节），
    需要"换底层而不中断界面"时应走后续的稳定门面（RPC 服务）而不是硬依赖。
    """
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    old = booted.ctx.require(WEBUI_SERVICE)

    booted.ctx.unload_plugin("fs:MemoryFS")
    assert "webui" in booted.ctx.pending_plugins  # 依赖链断开：一并退位
    assert not _port_open(old.port)

    booted.ctx.load_plugin(local_fs_plugin(tmp_path / "ws2"))
    new = booted.ctx.require(WEBUI_SERVICE)
    assert _port_open(new.port)  # 重新激活：服务回来了
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_index_page_includes_rich_renderer(tmp_path: Path):
    """页面自带富渲染：Markdown 表格/标题/引用 + HTML/CSS 代码块沙箱预览。

    回归背景：模型常用 Markdown 表格列能力清单，早期渲染器只认段落/列表/
    代码块，表格会原样显示成 `| # | 工具 |` 一堆竖线——这里把渲染能力钉住。
    """
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    _, html = _get_text(url)
    # Markdown 富渲染：CSS 有表格/引用/标题样式，JS 有表格与标题的行级识别
    assert "tablewrap" in html and "prose th" in html
    assert "blockquote" in html
    assert "isSeparator" in html                       # 表格识别逻辑
    assert "match(/^(#{1,6})" in html                  # 标题识别
    # HTML/CSS 代码块 → 沙箱预览
    assert "preview-btn" in html and "previewbox" in html
    assert 'setAttribute("sandbox", "allow-scripts")' in html  # 只给脚本权限
    assert "allow-same-origin" not in html.split("setAttribute", 1)[1][:60]
    booted.ctx.unload_plugin("webui")
    booted.close()
