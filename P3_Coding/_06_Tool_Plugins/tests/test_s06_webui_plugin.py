"""P3 界面插件测试：装载即起服务、卸载即停服、只读 API、流式对话。

全部走真实 HTTP（起在 127.0.0.1 的随机端口上，port=0 由系统分配）——
"界面是插件"的验收就是"装载能在浏览器里用、卸载不留痕迹"。
"""

from __future__ import annotations

import json
import re
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
        "tool-admin",           # `_06`：工具面管理
        "tools.surface",        # `_06`：工具管理的管理页自述
        "token-usage",          # `_03`
        "plugin-admin",         # `_05` 收尾：插件管理
        "plugin-admin.surface",
        "context-meter",        # `_04`
        "compacter",            # `_05`
        "compact.surface",      # `_05`：压缩插件的界面自述
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
    # 壳内置的只有这两个**通用协议**帧（壳与循环之间的约定）。
    # 插件自己的收尾帧（如压缩的 `__compact_done`）**不在壳里**——它们由插件
    # 自述（DisplaySpec.terminal），前端用 terminalFrames() 合并：
    # 于是"加一个返回结果的插件端"不用改壳。
    assert terminals == {"__done", "__error"}
    assert "function terminalFrames()" in js, "应合并插件自述的收尾帧"
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


def test_every_dom_id_app_js_reaches_for_exists_in_index_html():
    """静态契约：`app.js` 取用的每个元素 id，`index.html` 里都得有。

    回归背景：拆三件套时 `#banner` 元素掉了两回，而 `showBanner()` 只在
    用户点插件操作按钮时才被调到——于是页面看着一切正常，一点"安装"就
    `null.textContent` 炸掉，报错还只在浏览器控制台里。这类"少个 DOM 节点"
    的错不该靠人肉点一遍才发现，所以把两者对一遍。
    """
    root = Path(__file__).resolve().parent.parent / "webui"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")

    referenced: set[str] = set()
    for pattern in (
        r'\$\("([\w-]+)"\)',                    # 页面内的 $(id) 简写
        r'getElementById\("([\w-]+)"\)',
        r'querySelector\("#([\w-]+)"\)',
    ):
        referenced |= set(re.findall(pattern, js))
    declared = set(re.findall(r'id="([\w-]+)"', html))

    missing = sorted(referenced - declared)
    assert not missing, (
        f"app.js 用到的这些 id 在 index.html 里不存在：{missing}"
        f"（典型症状：点按钮时控制台报 null，界面无反应）"
    )


# ---------------------------------------------------------------------------
# 多会话并行时不串台（前端行为，用 Node 真跑那段逻辑）
# ---------------------------------------------------------------------------

_SINK_PROBE = r"""
import { readFileSync } from "node:fs";
const js = readFileSync(process.argv[2], "utf8");
const m = js.match(/function frameSinkFor\(sessionId\) \{[\s\S]*?\n\}/);
if (!m) { console.error("NO_MATCH"); process.exit(2); }

// 用桩件把这段函数真跑起来（不是读源码断言字符串）：
//   state.current = 当前显示的会话；handleFrame = 画聊天区；appendLiveEvent = 画事件流
const state = { current: "A" };
const rendered = [], events = [], working = new Set(["A", "B"]);
const handleFrame = (kind, payload) => {
  rendered.push(payload.text ?? "(control)");
  // 真实代码里的两个副作用（见 app.js）：turn_end 收掉"思考中"指示，
  // turn_start 起一个。**这正是"别人的 turn_end 会把我这边的指示抹掉"的机制。**
  if (kind === "turn_end") working.delete(state.current);
  if (kind === "turn_start") working.add(state.current);
};
const appendLiveEvent = (kind, payload) => events.push(payload.text ?? "(control)");
const frameSinkFor = eval("(" + m[0] + ")");

const sinkA = frameSinkFor("A"), sinkB = frameSinkFor("B");
sinkA("step_delta", { text: "甲的流" });        // 看着 A，A 的帧 → 显示
sinkB("step_delta", { text: "乙的流" });        // 看着 A，B 的帧 → **必须丢弃**
sinkA("__progress", {});                        // 控制帧：显示但**不进**事件流
state.current = "B";                            // 用户切到 B
sinkA("step_delta", { text: "甲还在跑" });      // A 的帧 → **必须丢弃**
sinkA("turn_end", {});                          // A 跑完了：**不能**动 B 的界面
const bIndicatorAfterForeignTurnEnd = working.has("B");
sinkB("step_delta", { text: "乙的流2" });       // B 的帧 → 显示
console.log(JSON.stringify({ rendered, events, bIndicatorAfterForeignTurnEnd }));
"""


def _run_node_sink_probe(js_path) -> dict:
    """在 Node 里真跑 app.js 的 frameSinkFor（前端逻辑的行为测试）。

    静态断言（"字符串在不在文件里"）抓不住"判断写错了"这类 bug——而这次报告的
    "多会话串台"正是判断缺失。所以这里把那段函数**执行**起来看结果。
    """
    import json as _json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过前端行为测试")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mjs"
        probe.write_text(_SINK_PROBE, encoding="utf-8")
        result = subprocess.run(
            [node, str(probe), str(js_path)],
            capture_output=True, text=True, timeout=60,
        )
    assert result.returncode == 0, f"Node 跑失败：{result.stderr[:400]}"
    return _json.loads(result.stdout)


def test_frames_are_dropped_when_viewing_another_session():
    """**多会话并行时不串台**：只渲染"当前显示的那条会话"的帧（真实报告）。

    症状（用户原话："在多个会话同步进行的时候会话的展示内容会发生混乱"）：
    A 还在跑，用户切到 B，A 的流式文本继续长进 B 的聊天区（右栏事件流也一样）。

    根因：所有推流都用同一个 `handleFrame` 直接渲染，**帧里不带会话信息**，
    渲染函数也不问"我该不该画"。切会话只是换了显示，订阅照旧在推。

    修法：`frameSinkFor(sessionId)` 造出"属于某条会话"的写帧端，两道闸门——
    不是当前会话的帧整帧丢弃（服务端照常跑完，内容都在日志里，切回去重渲染）；
    事件流与聊天区一起受同一判断管。

    本用例**执行**那段逻辑（Node），而不是读源码字符串。
    """
    root = Path(__file__).resolve().parent.parent / "webui"
    out = _run_node_sink_probe(root / "app.js")
    assert out["rendered"] == ["甲的流", "(control)", "乙的流2"], (
        f"渲染的帧不对：{out['rendered']}"
        "（预期：A 的帧 → 控制帧 → 切到 B 后 B 的帧；别的都该被丢弃）"
    )
    assert out["events"] == ["甲的流", "乙的流2"], (
        f"事件流收到的帧不对：{out['events']}（控制帧 `__` 开头不该进事件流）"
    )
    # **同一个根因的第二个症状**（用户另一次报告）："切换对话时正在运行标志
    # 只有第一个起效，其他不出现"。机制是——别的会话的 `turn_end` 帧被画进了
    # 当前视图，而它的处理里有一句 `removeWorking()`，于是把**你正在看的这条
    # 会话**的"思考中/运行中"指示一并抹掉。修好串台，指示自然跟着好。
    assert out["bIndicatorAfterForeignTurnEnd"] is True, (
        "别的会话的 turn_end 把当前会话的'运行中'指示抹掉了——"
        "这正是'标志只有第一个起效'的成因"
    )


def test_every_stream_call_site_binds_a_session():
    """所有推流入口都必须用**带会话的 sink**，不能再把裸 `handleFrame` 传进去。

    这条是上面那条的"覆盖面"补充：`frameSinkFor` 写对了，但如果某个入口
    （send / turn 接回 / compact…）漏用它，那条路径照样串台。
    """
    root = Path(__file__).resolve().parent.parent / "webui"
    js = (root / "app.js").read_text(encoding="utf-8")
    # 调用可能跨多行（`streamFrames(` 后面换行写参数），所以按**语句**扫：
    # 先把 `await <call>(...)` 整段抠出来（用括号配平找到收尾括号）。
    offenders: list[str] = []
    for match in re.finditer(r"await\s+(streamFrames|readNDJSON)\s*\(", js):
        start = match.end()                       # 开括号之后
        depth, i = 1, start
        while i < len(js) and depth:
            if js[i] == "(":
                depth += 1
            elif js[i] == ")":
                depth -= 1
            i += 1
        args = js[start : i - 1]
        if "frameSinkFor(" not in args:
            offenders.append(f"{match.group(1)}({args.strip()[:80]})")
    assert not offenders, (
        "这些推流调用点没把帧绑定到会话（多会话并行时会串台）：\n  "
        + "\n  ".join(offenders)
    )
