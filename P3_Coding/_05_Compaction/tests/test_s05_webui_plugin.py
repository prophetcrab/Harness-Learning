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


def test_webui_starts_without_dependencies_and_degrades(tmp_path: Path):
    """`_05` 起：agent / sessions 是**可选**依赖 —— 缺了照样起服务，只是降级。

    旧行为（本用例的前身）是"缺 agent 就 pending、界面根本不出现"。那让
    换模型这一幕很糟：agent 依赖 llm，模型一装卸 llm，界面就跟着消失
    （端口关闭、当前页面失联）。现在界面永不因别人的来去被重启，缺服务时
    由各路由给出 503 + 人话原因。
    """
    import json as _json
    import urllib.error
    import urllib.request

    from kernel import Context

    ctx = Context()
    ctx.load_plugin(webui_plugin(port=0))
    assert ctx.pending_plugins == []                      # 不再 pending
    info = [p for p in ctx.plugins if p.name == "webui"][0]
    assert info.status == "active"
    assert info.inject == ()                              # 没有必需依赖
    # 可选依赖：跑 turn 的 + 展示用的，全都缺了也不许拦着界面起来
    assert set(info.optional) == {"agent", "sessions", "approval", "tools"}
    assert set(info.missing_optionals) == {"agent", "sessions", "approval", "tools"}

    url = ctx.require(WEBUI_SERVICE).url
    with urllib.request.urlopen(url, timeout=8) as response:
        assert response.status == 200                     # 主页照常
    try:
        urllib.request.urlopen(url + "api/sessions", timeout=5)
        raise AssertionError("缺 sessions 时本该 503")
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
        assert "未就绪" in _json.loads(exc.read().decode("utf-8"))["error"]


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
        "compacter",            # `_05`
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


def test_webui_survives_upstream_plugin_swap(tmp_path: Path):
    """`_05` 的核心验收：**换底层插件时界面不中断、端口不变**。

    旧行为（本用例的前身）是"换 fs → webui 被迫退位/重启、端口更换"，当时
    当作已知边界记录在案。但它其实是选错了声明方式：界面对 agent 的依赖
    写成 inject 之后，模型装卸 llm（agent 的上游）会把整个界面连带停掉——
    用户眼前的页面当场失联。改成 optional 后，这一串级联不再发生。

    这里用 fs 做触发器（与 llm 同理：fs 是 agent 链条之外但被 tools 依赖的
    上游），断言"服务在、端口没变、任务照跑"。
    """
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    old = booted.ctx.require(WEBUI_SERVICE)
    assert _port_open(old.port)

    booted.ctx.unload_plugin("fs:MemoryFS")
    assert "webui" in booted.ctx.active_plugins        # 界面不再被连带停用
    assert _port_open(old.port)                        # 端口没变、服务还在

    booted.ctx.load_plugin(local_fs_plugin(tmp_path / "ws2"))
    same = booted.ctx.require(WEBUI_SERVICE)
    assert same.port == old.port                       # 地址不变（标签页不用刷新）
    assert _port_open(same.port)

    # 而且界面仍能正常干活：换完 fs 之后跑一个 turn
    frames = _post_ndjson(old.url + "api/sessions/swap/send", {"text": "换完底层再来一句"})
    assert any(f["kind"] == "__done" for f in frames), "换完 fs 后界面应仍能跑 turn"
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_static_assets_served_with_correct_types(tmp_path: Path):
    """三件套各按自己的 Content-Type 发出（拆文件后新增的路由）。"""
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    for path, marker, expected_type in (
        ("", "<!DOCTYPE html>", "text/html"),
        ("index.html", "plugin-panel", "text/html"),   # `_05`：通用面板容器
        ("app.css", ":root{", "text/css"),
        ("app.js", "use strict", "text/javascript"),
    ):
        request = urllib.request.Request(url + path)
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read().decode("utf-8")
            assert response.headers["Content-Type"].startswith(expected_type), path
        assert marker in body, f"{path} 内容不对：找不到 {marker}"
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_static_route_refuses_path_traversal(tmp_path: Path):
    """静态资源是**白名单**：任何白名单外的名字一律 404，绝不做路径拼接。

    这是一台 HTTP 服务（虽只绑本机），路径穿越必须挡在门口——白名单里的
    名字全是常量，请求给不出新路径，攻击面直接不存在。
    """
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    for bad in ("../plugins/webui.py", "..%2f..%2fsecret", "app.js/../../plugins",
                "/app.js/../index.html", "plugins/webui.py", "secret.txt"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(url + bad, timeout=8)
        assert excinfo.value.code == 404, f"{bad} 没被拒绝"
    booted.ctx.unload_plugin("webui")
    booted.close()


def test_stream_frames_forwards_approval_to_the_page(tmp_path: Path):
    """`__approval` 必须送到页面渲染——**否则整个 turn 卡死在等审批上**。

    回归背景（真实故障，三处症状同一个根因）：拆页面时把"所有 `__` 开头的帧
    都当收尾帧留在 `last` 里、不交给 handleFrame"，于是：
      · 审批卡片永远不出现（用户看不到要点批准）；
      · 服务端等 300 秒超时后按 fail-closed 拒绝 → 表现为"工具莫名失败"；
      · turn 结束前页面不刷新 → "要手动刷新才看到进度"。
    测试直接读 app.js 的源码约定：只有**明确列出的收尾帧**才跳过渲染。
    """
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url
    _, js = _get_text(url + "app.js")

    assert "TERMINAL_FRAMES" in js, "应有显式的收尾帧清单，而不是按前缀一刀切"
    # 解析出白名单本身，逐项检查（比"字符串在不在文件里"精确得多——
    # `"__approval"` 也会出现在 handleFrame 的 case 里，那是**渲染**它的地方）
    import re

    match = re.search(r"const TERMINAL_FRAMES = new Set\(\[(.*?)\]\)", js)
    assert match, "应由 TERMINAL_FRAMES 显式列出收尾帧"
    terminals = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert terminals == {"__done", "__error", "__compact_done", "__compact_noop"}
    # 关键：审批 / 修复**不在**白名单里（它们必须被渲染出来）
    assert "__approval" not in terminals, "__approval 被当成收尾帧吞掉了"
    assert "__repair" not in terminals, "__repair 被当成收尾帧吞掉了"
    # 而页面确实有渲染它们的处理分支
    assert 'case "__approval"' in js and 'case "__repair"' in js
    booted.close()


def test_index_page_includes_rich_renderer(tmp_path: Path):
    """页面自带富渲染：Markdown 表格/标题/引用 + HTML/CSS 代码块沙箱预览。

    回归背景：模型常用 Markdown 表格列能力清单，早期渲染器只认段落/列表/
    代码块，表格会原样显示成 `| # | 工具 |` 一堆竖线——这里把渲染能力钉住。
    """
    booted = _boot(tmp_path, [text_reply("好")])
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    # `_05` 起页面拆成三件套（index.html + app.css + app.js）：各读其源文件
    _, html = _get_text(url)
    _, css = _get_text(url + "app.css")
    _, js = _get_text(url + "app.js")
    assert 'href="app.css"' in html and 'src="app.js"' in html
    # Markdown 富渲染：CSS 有表格/引用/标题样式，JS 有表格与标题的行级识别
    assert "tablewrap" in css and "prose th" in css
    assert "blockquote" in css
    assert "isSeparator" in js                         # 表格识别逻辑
    assert "match(/^(#{1,6})" in js                    # 标题识别
    # HTML/CSS 代码块 → 沙箱预览
    assert "preview-btn" in js and "previewbox" in css
    assert 'setAttribute("sandbox", "allow-scripts")' in js     # 只给脚本权限
    assert "allow-same-origin" not in js.split("setAttribute", 1)[1][:60]
    booted.ctx.unload_plugin("webui")
    booted.close()
