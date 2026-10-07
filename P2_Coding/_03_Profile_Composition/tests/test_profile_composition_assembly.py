"""_03_Profile_Composition 的基线验收测试：单元级、全离线、FakeLLM 驱动（M1–M3 组装回归）。

覆盖六层（对应 M1–M3 的组装验收）：
1. 装配：MiniHarness.open 建会话、装工具、管线接入循环
2. 完整闭环：calculate 工具调用 → 结果 → 最终回答，且事件落盘
3. 审批 + 沙箱：write_file 批准/拒绝/越界三条路径
4. resume：同 session_id 重开 → 历史完整、turn 接续、文件仍在
5. 同构：重放日志 == 在线历史；崩溃尾部修复
6. CLI：run / list / show / fork 子命令

运行：cd P2_Coding/_03_Profile_Composition && python -m pytest -q
"""


import pytest

from harness.llm import FakeLLM, text_reply, tool_call_reply
from harness.mini import MiniHarness
from harness.session import JsonlStore
from harness.tools import AutoApprove, AutoDeny, ScriptedApprover
from harness.tools.approval import ApprovalDecision
from harness.tools.workspace import build_workspace_registry

# =========================================================================
# 1) 装配
# =========================================================================


def test_open_builds_session_and_tools(tmp_path):
    """open：建会话、注册默认工具、session/start 落日志。"""
    harness, report = MiniHarness.open(
        "s1",
        provider=FakeLLM([text_reply("hi")]),
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        system_prompt="系统提示",
    )
    assert report.repaired is False
    assert harness.session.has_started
    names = [spec.name for spec in harness.tools]
    assert names[:4] == ["calculate", "read_file", "write_file", "list_files"]
    # system 进了日志、且能投影出来
    assert harness.messages[0].role == "system"
    assert harness.messages[0].content == "系统提示"


def test_search_tool_is_optional(tmp_path):
    """--search 才注册 web_search；默认不注册（保持离线）。"""
    default = build_workspace_registry(tmp_path / "ws")
    assert "web_search" not in default.names
    with_search = build_workspace_registry(tmp_path / "ws", include_search=True)
    assert "web_search" in with_search.names


# =========================================================================
# 2) 完整闭环（管线接入循环）
# =========================================================================


def test_full_loop_with_tool_and_logging(tmp_path):
    """模型申请 calculate → 管线执行 → 结果回填 → 最终回答；事件逐条落盘。"""
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("结果是 70066.52"),
        ]
    )
    harness, _ = MiniHarness.open(
        "loop", provider=provider, root=tmp_path / "sessions", workspace=tmp_path / "ws"
    )
    result = harness.send("帮我算 1234*56.78")

    assert result.status == "done"
    assert result.final_text == "结果是 70066.52"
    assert len(result.steps) == 2
    assert result.steps[0].tool_results[0].result["result"] == pytest.approx(70066.52)
    provider.assert_all_consumed()

    # 事件确实落了盘，且包含工具往返
    events, _ = JsonlStore(tmp_path / "sessions").load("loop")
    types = [e.type for e in events]
    assert "session/start" in types
    assert "tool/result" in types
    assert types.count("turn/end") == 1


# =========================================================================
# 3) 审批 + 沙箱
# =========================================================================


def test_write_file_approved_persists(tmp_path):
    """write_file 需审批：批准后文件真正落盘。"""
    workspace = tmp_path / "ws"
    provider = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hello"}),
            text_reply("已写入。"),
        ]
    )
    harness, _ = MiniHarness.open(
        "w1",
        provider=provider,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
    )
    result = harness.send("写个文件")

    assert result.status == "done"
    assert (workspace / "notes" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_denied_does_not_persist(tmp_path):
    """拒绝后工具不执行，文件不落盘，模型拿到拒绝原因。"""
    workspace = tmp_path / "ws"
    provider = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hello"}),
            text_reply("好的，不写了。"),
        ]
    )
    harness, _ = MiniHarness.open(
        "w2",
        provider=provider,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoDeny("用户拒绝"),
    )
    result = harness.send("写个文件")

    assert result.status == "done"
    assert not (workspace / "notes" / "a.txt").exists()
    tool_result = result.steps[0].tool_results[0]
    assert tool_result.error is True
    assert "denied" in tool_result.result


def test_sandbox_blocks_escape_even_when_approved(tmp_path):
    """沙箱是第二道防线：审批放行也不能越界写。"""
    workspace = tmp_path / "ws"
    provider = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "../escape.txt", "content": "x"}),
            text_reply("被拒绝了。"),
        ]
    )
    harness, _ = MiniHarness.open(
        "w3",
        provider=provider,
        root=tmp_path / "sessions",
        workspace=workspace,
        approval=AutoApprove(),
    )
    result = harness.send("越界写")

    assert not (tmp_path / "escape.txt").exists()
    assert "越出工作区" in result.steps[0].tool_results[0].result["error"]


def test_approval_scripted_matches_ask_count(tmp_path):
    """审批剧本精确匹配：被问一次、批准一次。"""
    approver = ScriptedApprover([ApprovalDecision(True, "批准")])
    provider = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "a.txt", "content": "x"}),
            text_reply("ok"),
        ]
    )
    harness, _ = MiniHarness.open(
        "w4",
        provider=provider,
        root=tmp_path / "sessions",
        workspace=tmp_path / "ws",
        approval=approver,
    )
    harness.send("写")
    assert approver.request_count == 1
    approver.assert_all_consumed()


# =========================================================================
# 4) resume
# =========================================================================


def test_resume_restores_history_and_continues_turns(tmp_path):
    """同 session_id 重开：历史完整、turn 接续、之前写的文件仍在。"""
    root, workspace = tmp_path / "sessions", tmp_path / "ws"
    p1 = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "notes/a.txt", "content": "hi"}),
            text_reply("写好了。"),
        ]
    )
    h1, _ = MiniHarness.open("s", provider=p1, root=root, workspace=workspace, approval=AutoApprove())
    h1.send("写文件")
    assert (workspace / "notes" / "a.txt").is_file()

    # 模拟退出：只留磁盘日志，重新打开
    p2 = FakeLLM([text_reply("在的。")])
    h2, report = MiniHarness.open("s", provider=p2, root=root, workspace=workspace)
    assert report.repaired is False
    # 历史完整（含之前 turn 的消息），system 只注入一次
    roles = [m.role for m in h2.messages]
    assert roles[0] == "system"
    assert roles.count("system") == 1
    assert "tool" in roles

    result = h2.send("还记得吗？")
    assert result.turn == 2  # 接续，不是从 1 重来
    assert h2.session.last_turn_number == 2


def test_workspace_persists_across_sessions(tmp_path):
    """resume 后文件工具能看到上一段会话写的文件。"""
    root, workspace = tmp_path / "sessions", tmp_path / "ws"
    p1 = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "a.txt", "content": "hello"}),
            text_reply("ok"),
        ]
    )
    h1, _ = MiniHarness.open("s", provider=p1, root=root, workspace=workspace, approval=AutoApprove())
    h1.send("写")

    p2 = FakeLLM(
        [
            tool_call_reply("read_file", {"path": "a.txt"}),
            text_reply("内容是 hello"),
        ]
    )
    h2, _ = MiniHarness.open("s", provider=p2, root=root, workspace=workspace)
    result = h2.send("读回来")
    assert result.steps[0].tool_results[0].result["content"] == "hello"


# =========================================================================
# 5) 同构 + 崩溃修复
# =========================================================================


def test_isomorphism_replay_equals_online(tmp_path):
    """重放日志得到的历史 == 在线产生的历史。"""
    from harness.runner import load_messages

    root, workspace = tmp_path / "sessions", tmp_path / "ws"
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "2+2"}),
            text_reply("4"),
        ]
    )
    harness, _ = MiniHarness.open("iso", provider=provider, root=root, workspace=workspace)
    harness.send("2+2")

    def plain(msgs):
        return [(m.role, m.content, tuple((c.id, c.name) for c in m.tool_calls), m.tool_call_id) for m in msgs]

    replayed = plain(load_messages("iso", JsonlStore(root)))
    assert replayed == plain(harness.history)


def test_open_reports_crash_repair(tmp_path):
    """往 jsonl 尾部写半行 → 重新 open 时报告 repaired。"""
    root, workspace = tmp_path / "sessions", tmp_path / "ws"
    provider = FakeLLM([text_reply("hi")])
    h1, _ = MiniHarness.open("crash", provider=provider, root=root, workspace=workspace)
    h1.send("你好")

    path = JsonlStore(root).path("crash")
    path.write_bytes(path.read_bytes() + b'{"seq": 99, "type": "user/mess')

    p2 = FakeLLM([])
    h2, report = MiniHarness.open("crash", provider=p2, root=root, workspace=workspace)
    assert report.repaired is True
    assert report.dropped_bytes > 0


# =========================================================================
# 6) CLI
# =========================================================================


def test_cli_run_list_show_fork(tmp_path, capsys):
    """CLI：run 跑一个 turn、list 列出、show 打印事件、fork 分叉。"""
    import harness.cli as app

    root = tmp_path / "sessions"
    workspace = tmp_path / "ws"

    rc = app.main(
        [
            "run", "帮我算 2+3", "--fake",
            "--session", "cli1",
            "--root", str(root),
            "--workspace", str(workspace),
            "--no-approve",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "最终回答" in out

    assert app.main(["list", "--root", str(root)]) == 0
    assert "cli1" in capsys.readouterr().out

    assert app.main(["show", "cli1", "--root", str(root)]) == 0
    assert "session/start" in capsys.readouterr().out

    assert app.main(["fork", "cli1", "cli-fork", "--upto", "3", "--root", str(root)]) == 0
    assert "cli-fork" in capsys.readouterr().out

    # 分叉出的会话存在且事件更少
    forked_events, _ = JsonlStore(root).load("cli-fork")
    original_events, _ = JsonlStore(root).load("cli1")
    assert 0 < len(forked_events) <= len(original_events)


def test_cli_fork_refuses_existing_target(tmp_path):
    """fork 目标已存在 → fail loud，不覆盖。"""
    import harness.cli as app

    root = tmp_path / "sessions"
    store = JsonlStore(root)
    from harness.session import Session

    session = Session("base", store=store, events=[])
    session.append("session/start", {"session_id": "base"})

    with pytest.raises(FileExistsError):
        app.main(["fork", "base", "base", "--root", str(root)])
