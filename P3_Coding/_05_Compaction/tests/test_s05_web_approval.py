"""`_01` 浏览器审批 + 从工作区安装插件（install_plugin）的测试。

验收形状：
- 审批三态：批准 → 生效；拒绝 → 零副作用；无应答（超时/未连接）→ fail-closed；
- `install_plugin` 能安装工作区里的新插件代码，装入后出现在台账、工具面、
  并进入可装载/可卸载白名单；
- 审批关闭时（拒绝）安装路径什么都不会发生。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from components.llm import text_reply, tool_call_reply
from components.tools import ApprovalDecision, ApprovalRequest, ScriptedApprover
from plugins import (
    APPROVAL_SERVICE,
    SELF_TOOLS_SERVICE,
    TOOLS_SERVICE,
    boot_offline,
    web_approval_plugin,
)
from plugins.self_tools import SelfToolsService  # noqa: F401  （类型提示用）
from plugins.web_approval import WebApprovalPolicy

PLUGIN_SRC = '''
import time


class Clock2Plugin:
    name = "clock2"

    def setup(self, ctx):
        from components.tools import define_tool

        registry = ctx.require("tools")

        def stamp() -> dict:
            return {"stamp": time.strftime("%H:%M:%S")}

        registry.register(define_tool("stamp", "返回当前时间戳"), stamp)
        return lambda: registry.unregister("stamp")


def factory():
    return Clock2Plugin()
'''


def _boot(tmp_path: Path, **kwargs):
    return boot_offline(
        root=tmp_path / "sessions", workspace=tmp_path / "ws",
        script=None, self_tools=True, **kwargs,
    )


def _attach_ui(policy: WebApprovalPolicy) -> list[tuple[str, dict]]:
    """模拟"浏览器连着"：挂一个收集帧的 sender，返回帧列表。

    没有连接时策略按设计 fail-closed（直接拒绝、不建 pending）——所以凡是
    要走到"等用户点击"这一步的用例，都得先有这条连接。
    """
    frames: list[tuple[str, dict]] = []
    policy.attach_stream(lambda kind, payload: frames.append((kind, payload)))
    return frames


def _auto_approve(policy: WebApprovalPolicy, *, approved: bool = True, timeout: float = 5.0):
    """后台守着 pending 列表，出现就替"用户"点一下。"""
    def worker() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pending = policy.pending_list()
            if pending:
                policy.resolve(pending[0]["id"], approved, "测试自动点击")
                return
            time.sleep(0.05)
    thread = threading.Thread(target=worker)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# 浏览器审批策略本身
# ---------------------------------------------------------------------------


def test_policy_fail_closed_without_stream():
    """没人连着（没界面）→ 直接拒绝，不等超时。"""
    policy = WebApprovalPolicy(timeout=5.0)
    decision = policy.approve(ApprovalRequest(tool_name="write_file", arguments={}, reason="测试"))
    assert decision.allowed is False
    assert "fail-closed" in decision.reason


def test_policy_resolve_approve_and_deny():
    policy = WebApprovalPolicy(timeout=5.0)
    frames: list[tuple[str, dict]] = []
    policy.attach_stream(lambda kind, payload: frames.append((kind, payload)))

    _auto_approve(policy, approved=True)
    decision = policy.approve(
        ApprovalRequest(tool_name="load_plugin", arguments={"name": "clock"}, reason="需要审批")
    )
    assert decision.allowed is True
    assert frames and frames[0][0] == "__approval"
    assert frames[0][1]["tool"] == "load_plugin"
    assert policy.pending_list() == []          # 已结清


def test_policy_timeout_is_fail_closed():
    policy = WebApprovalPolicy(timeout=0.15)     # 极短超时，无人应答
    policy.attach_stream(lambda kind, payload: None)
    start = time.time()
    decision = policy.approve(ApprovalRequest(tool_name="shell", arguments={}, reason="测试超时"))
    assert decision.allowed is False
    assert "超时" in decision.reason
    assert time.time() - start < 3               # 没有硬等


def test_policy_ignores_unknown_resolution():
    policy = WebApprovalPolicy(timeout=1.0)
    assert policy.resolve("apv-nope", True) is False


# ---------------------------------------------------------------------------
# install_plugin：从工作区安装新代码
# ---------------------------------------------------------------------------


def test_install_plugin_loads_code_and_registers_whitelist(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    booted.ctx.require("fs").write_text("plugins/clock2.py", PLUGIN_SRC)

    frames = _attach_ui(policy)
    thread = _auto_approve(policy)
    result = service.install("plugins/clock2.py")
    thread.join(timeout=3)
    assert any(kind == "__approval" for kind, _ in frames)   # 确实弹了审批

    assert result["installed"] == "clock2"
    assert result["status"] == "active"
    assert result["tools_added"] == ["stamp"]
    assert result["registered_in_whitelist"] is True
    # 台账（左侧栏的数据源）里出现它
    assert any(p.name == "clock2" for p in booted.ctx.plugins)
    # 白名单被动态追加
    assert "clock2" in service.loadable and "clock2" in service.unloadable
    # 新工具真的能调用
    tool = booted.ctx.require(TOOLS_SERVICE).resolve("stamp")
    assert tool is not None and "stamp" in tool.func()
    booted.close()


def test_install_plugin_rejected_changes_nothing(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    booted.ctx.require("fs").write_text("plugins/clock2.py", PLUGIN_SRC)

    before_tools = list(booted.ctx.require(TOOLS_SERVICE).names)
    _attach_ui(policy)
    thread = _auto_approve(policy, approved=False)
    result = service.install("plugins/clock2.py")
    thread.join(timeout=3)

    assert result["code"] == "PLUGIN_ACCESS_DENIED" or result.get("error")
    assert list(booted.ctx.require(TOOLS_SERVICE).names) == before_tools
    assert not any(p.name == "clock2" for p in booted.ctx.plugins)
    assert "clock2" not in service.loadable      # 白名单没被污染
    booted.close()


def test_install_plugin_rejects_non_python(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    booted.ctx.require("fs").write_text("notes.txt", "hello")
    result = service.install("notes.txt")
    assert result["code"] == "INSTALL_BAD_PATH"
    booted.close()


def test_install_plugin_reports_missing_entry(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    booted.ctx.require("fs").write_text("plugins/broken.py", "x = 1\n")  # 没有插件入口

    _attach_ui(policy)
    thread = _auto_approve(policy)
    result = service.install("plugins/broken.py")
    thread.join(timeout=3)
    assert result["code"] == "INSTALL_PARSE_FAILED"
    assert "找不到插件入口" in result["error"]
    assert "broken" not in service.loadable
    booted.close()


def test_install_plugin_rolls_back_whitelist_on_load_failure(tmp_path: Path):
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    bad = '''
class BoomPlugin:
    name = "boom"

    def setup(self, ctx):
        raise RuntimeError("装配时炸了")


def factory():
    return BoomPlugin()
'''
    booted.ctx.require("fs").write_text("plugins/boom.py", bad)
    before = list(booted.ctx.require(TOOLS_SERVICE).names)

    _attach_ui(policy)
    thread = _auto_approve(policy)
    result = service.install("plugins/boom.py")
    thread.join(timeout=3)

    assert result["code"] == "PLUGIN_LOAD_FAILED"
    assert "boom" not in service.loadable           # 白名单登记已回滚
    assert list(booted.ctx.require(TOOLS_SERVICE).names) == before
    booted.close()


def test_install_plugin_preview_shows_source(tmp_path: Path):
    """审批人能看到源码（这是"看着代码批准"的关键）。"""
    booted = _boot(tmp_path)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    booted.ctx.require("fs").write_text("plugins/clock2.py", PLUGIN_SRC)

    _attach_ui(policy)
    seen: list[dict] = []

    def worker() -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = policy.pending_list()
            if pending:
                seen.append(pending[0])
                policy.resolve(pending[0]["id"], False, "看一眼就够了")
                return
            time.sleep(0.05)

    thread = threading.Thread(target=worker)
    thread.start()
    booted.ctx.require(SELF_TOOLS_SERVICE).install("plugins/clock2.py")
    thread.join(timeout=3)

    assert seen, "应收到一次审批请求"
    assert seen[0]["preview"] and "Clock2Plugin" in seen[0]["preview"]
    booted.close()


def test_colon_load_and_install_share_whitelist(tmp_path: Path):
    """install 之后 load 同一名字可用（装载与卸载走同一份白名单）。"""
    booted = _boot(tmp_path)
    service = booted.ctx.require(SELF_TOOLS_SERVICE)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    booted.ctx.require("fs").write_text("plugins/clock2.py", PLUGIN_SRC)

    _attach_ui(policy)
    thread = _auto_approve(policy)
    service.install("plugins/clock2.py")
    thread.join(timeout=3)

    unloaded = service.unload("clock2")
    assert unloaded["unloaded"] == "clock2" and "stamp" in unloaded["tools_removed"]

    reloaded = service.load("clock2")          # 名字已在白名单里
    assert reloaded["loaded"] == "clock2" and reloaded["tools_added"] == ["stamp"]
    booted.close()


# ---------------------------------------------------------------------------
# 审批事件（供界面/RPC 观测）
# ---------------------------------------------------------------------------


def test_approval_events_emitted(tmp_path: Path):
    from plugins import APPROVAL_REQUESTED, APPROVAL_RESOLVED

    booted = _boot(tmp_path)
    policy = booted.ctx.require(APPROVAL_SERVICE)
    seen: list[tuple[str, dict]] = []
    booted.ctx.on(APPROVAL_REQUESTED, lambda **kw: seen.append(("requested", kw)))
    booted.ctx.on(APPROVAL_RESOLVED, lambda **kw: seen.append(("resolved", kw)))

    _attach_ui(policy)
    thread = _auto_approve(policy, approved=True)
    policy.approve(ApprovalRequest(tool_name="load_plugin", arguments={}, reason="测试"))
    thread.join(timeout=3)

    assert seen[0][0] == "requested" and seen[0][1]["pending"]["tool"] == "load_plugin"
    assert seen[1][0] == "resolved" and seen[1][1]["approved"] is True
    booted.close()


def test_explicit_approval_argument_still_wins(tmp_path: Path):
    """装配方显式给了策略时，走管线的工具用显式策略，不弹浏览器卡片。"""
    approver = ScriptedApprover([ApprovalDecision(True, "脚本批准")])
    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[tool_call_reply("load_plugin", {"name": "clock"}), text_reply("装好了")],
        self_tools=True, approval=approver,
    )
    assert booted.ctx.require(APPROVAL_SERVICE) is not None   # 服务在，但不该被用到
    result = booted.agent.ask("装 clock", session_id="s1")
    assert result.status == "done"
    approver.assert_all_consumed()                            # 显式策略被消费
    assert "clock" in booted.ctx.services                     # 真的装上了
    booted.close()


def test_web_approval_plugin_provides_service(tmp_path: Path):
    from kernel import Context

    ctx = Context()
    ctx.load_plugin(web_approval_plugin(timeout=1.0))
    assert APPROVAL_SERVICE in ctx.services
    assert isinstance(ctx.require(APPROVAL_SERVICE), WebApprovalPolicy)


@pytest.mark.parametrize("timeout", [0.2, 0.5])
def test_policy_timeout_parameter(tmp_path: Path, timeout: float):
    policy = WebApprovalPolicy(timeout=timeout)
    assert policy.timeout == timeout


# ---------------------------------------------------------------------------
# 推流通道的作用域（回归：曾把后来者的通道一并清掉）
# ---------------------------------------------------------------------------


def test_detach_only_clears_own_stream():
    """turn A 结束时的 detach 不能清掉 turn B 挂上的通道。

    真实踩到的 bug：turn A 长时间等审批（期间连接关闭），turn B 挂上自己的
    通道后 A 收尾——A 的 detach 把 B 的通道清成了 None，B 的审批随即误报
    "浏览器未连接"。修法是 detach 带句柄、只摘自己挂的那个。
    """
    policy = WebApprovalPolicy(timeout=0.2)
    a_frames: list = []
    b_frames: list = []
    handle_a = policy.attach_stream(lambda k, p: a_frames.append((k, p)))
    handle_b = policy.attach_stream(lambda k, p: b_frames.append((k, p)))

    policy.detach_stream(handle_a)          # A 收尾：不应影响 B

    _auto_approve(policy, approved=True, timeout=3)
    decision = policy.approve(
        ApprovalRequest(tool_name="install_plugin", arguments={}, reason="B 的审批")
    )
    assert decision.allowed is True          # B 的通道仍在，审批能走通
    assert b_frames and not a_frames         # 帧推给 B，不推给 A
    policy.detach_stream(handle_b)
    assert policy.pending_list() == []


def test_detach_without_handle_still_clears():
    """兼容旧用法：不传句柄就是无条件清空。"""
    policy = WebApprovalPolicy(timeout=0.2)
    policy.attach_stream(lambda k, p: None)
    policy.detach_stream()
    decision = policy.approve(ApprovalRequest(tool_name="shell", arguments={}, reason="x"))
    assert decision.allowed is False and "fail-closed" in decision.reason


def test_pending_approval_survives_a_refresh(tmp_path: Path):
    """**回归：刷新页面后，正在等待的审批要能补看到**（真 bug）。

    `__approval` 帧只在"等待开始"那一刻推给当时的订阅者。刷新页面＝换了订阅者，
    那张卡片就永远回不来了——而 turn 还在服务端**阻塞等它**（默认 300 秒），
    到点按 fail-closed 拒绝。用户看到的是"刷了个新，工具就莫名失败了"。

    修法：前端接回 turn 时调 `GET /api/approvals` 补看（后端这条路径一直有，
    只是没人调）。本用例验后端那一半：断开第一个订阅者后，待批仍在列表里，
    并能通过 HTTP 作答让 turn 继续。
    """
    from plugins import WEBUI_SERVICE, webui_plugin

    booted = boot_offline(
        root=tmp_path / "s", workspace=tmp_path / "ws",
        script=[tool_call_reply("write_file", {"path": "a.txt", "content": "hi"}),
                text_reply("写好了")],
    )
    booted.ctx.load_plugin(web_approval_plugin(timeout=30))
    booted.ctx.load_plugin(webui_plugin(port=0))
    url = booted.ctx.require(WEBUI_SERVICE).url

    seen: list[str] = []

    def client_that_leaves() -> None:
        """读到审批帧就断（= 用户按了刷新）。"""
        request = urllib.request.Request(
            url + "api/sessions/apv/send",
            data=json.dumps({"text": "写个文件"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            for line in response:
                if not line.strip():
                    continue
                frame = json.loads(line)
                seen.append(frame["kind"])
                if frame["kind"] == "__approval":
                    response.close()
                    return

    thread = threading.Thread(target=client_that_leaves, daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert "__approval" in seen, f"没等到审批帧：{seen}"

    # 刷新后的页面：补看
    with urllib.request.urlopen(url + "api/approvals", timeout=10) as response:
        pending = json.load(response)["pending"]
    assert [p["tool"] for p in pending] == ["write_file"], pending

    # 作答 → turn 继续跑完
    answer = urllib.request.Request(
        url + f"api/approvals/{pending[0]['id']}",
        data=json.dumps({"approved": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(answer, timeout=10) as response:
        assert json.load(response)["ok"] is True

    for _ in range(40):
        time.sleep(0.25)
        with urllib.request.urlopen(url + "api/sessions/apv/events", timeout=10) as response:
            events = json.load(response)["events"]
        ends = [e["data"]["status"] for e in events if e["type"] == "turn/end"]
        if ends:
            break
    assert ends == ["done"], f"批准后 turn 应正常收尾，实际 {ends}"
    booted.close()
