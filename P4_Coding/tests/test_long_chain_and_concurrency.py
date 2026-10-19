"""长链路 / 并行会话 / 会话内工作区编辑的功能验收（离线）。

这三类场景是"产品真的在干活"时才出现的组合，单元级验收覆盖不到：

    长链路          一步调工具、下一步基于结果继续，直到多轮任务完成
    并行会话        多个 agent 同时跑，日志/投影/策略互不串台
    会话内编辑      读 → 改 → 验，读后写与审批去重按**会话**记账

模型走剧本传输（不联网），其余链路（装配、工具、fs、策略、持久化）都是真的。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core import Context, boot
from manifests.minimal import catalog, manifest
from plugins.base.agent import AGENT_TOOL_CALL
from plugins.base.llm import ToolCall
from plugins.base.project import PROJECT_SERVICE
from plugins.base.sessions import SESSIONS_SERVICE, TOOL_RESULT
from plugins.base.tools_admin import TOOL_ADMIN_SERVICE
from plugins.capability.fs.observation_policy import fs_observation_policy_plugin
from plugins.capability.fs.sandbox import fs_sandbox_plugin

# ---------------------------------------------------------------------------
# 剧本传输
# ---------------------------------------------------------------------------


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}"


def script_transport(script: list[Any]):
    """按顺序吐出剧本：字符串 = 文本回答，ToolCall 列表 = 发起工具调用。

    同一轮里构造 payload 的**消息历史是真的**，正因如此它能验证"下一步能
    看到上一步的工具结果"——这正是长链路的核心。
    """
    state = {"index": 0}

    def transport(url: str, headers: Any, payload: Any, timeout: float):
        index = min(state["index"], len(script) - 1)
        state["index"] += 1
        step = script[index]
        if isinstance(step, str):
            events = [
                _sse({"choices": [{"delta": {"content": step}}]}),
                _sse(
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    }
                ),
            ]
        else:
            deltas = [
                {
                    "index": position,
                    "id": call.id,
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for position, call in enumerate(step)
            ]
            events = [
                _sse({"choices": [{"delta": {"tool_calls": deltas}}]}),
                _sse(
                    {
                        "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    }
                ),
            ]
        return [*events, "data: [DONE]"]

    return transport


def echo_transport():
    """把最后一条 user 消息原样回显——用于验证并行会话不串台。"""

    def transport(url: str, headers: Any, payload: Any, timeout: float):
        text = ""
        for message in reversed(payload.get("messages", [])):
            if message.get("role") == "user":
                content = message.get("content")
                text = content if isinstance(content, str) else ""
                break
        events = [
            _sse({"choices": [{"delta": {"content": text}}]}),
            _sse(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                }
            ),
        ]
        return [*events, "data: [DONE]"]

    return transport


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _boot(
    tmp_path: Path,
    *,
    transport: Any = None,
    sandbox: str = "free",
    approval: str = "allow",
    loop_config: dict[str, Any] | None = None,
) -> Any:
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    workspace.mkdir(parents=True, exist_ok=True)
    entries = manifest(
        workspace=workspace,
        sessions=sessions,
        sandbox_mode=sandbox,
        approval_policy=approval,
        replies=["（剧本）"],
    )
    for entry in entries[0]["children"]:
        if entry["id"] == "llm-provider" and transport is not None:
            entry["config"]["transport"] = transport
        if entry["id"] == "loop" and loop_config is not None:
            entry["config"] = dict(loop_config)
    return boot(entries, catalog=catalog())


def _agent(rt: Any, session_id: str, **options: Any):
    return rt.ctx.require(PROJECT_SERVICE).start(session_id=session_id, **options)


def _tool_messages(rt: Any, session_id: str) -> list[Any]:
    return [
        message
        for message in rt.ctx.require(SESSIONS_SERVICE).messages(session_id)
        if message.role == "tool"
    ]


def _assert_tool_calls_are_paired(messages: list[Any]) -> None:
    """每个 assistant.tool_calls 都必须在后面跟着对应 id 的 tool 消息。"""
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        expected = {call.id for call in message.tool_calls}
        actual = {
            later.tool_call_id
            for later in messages[index + 1 :]
            if later.role == "tool"
        }
        assert expected <= actual, f"悬空 tool_calls：{expected - actual}"


# ---------------------------------------------------------------------------
# 1. 长链路：多步工具链
# ---------------------------------------------------------------------------


def test_long_chain_of_eight_tool_steps(tmp_path: Path) -> None:
    """八步链路：写入 → 读 → 改 → 读 → 搜索 → 计算 → 列目录 → 收尾。

    每一步都依赖上一步的真实结果（文件内容、搜索结果、计算结果），验证的是
    "长链路里状态一直自洽"而不是单次工具调用。
    """
    rt = _boot(
        tmp_path,
        transport=script_transport(
            [
                # 1 写两个文件
                [ToolCall("s1", "write_file", '{"path":"src/app.py","content":"value=1\\n"}')],
                # 2 写入第二个文件
                [ToolCall("s2", "write_file", '{"path":"notes.md","content":"TODO: raise value\\n"}')],
                # 3 读回来
                [ToolCall("s3", "read_file", '{"path":"src/app.py"}')],
                # 4 改内容
                [
                    ToolCall(
                        "s4",
                        "edit_file",
                        '{"path":"src/app.py","old":"value=1","new":"value=2"}',
                    )
                ],
                # 5 再读确认
                [ToolCall("s5", "read_file", '{"path":"src/app.py"}')],
                # 6 全文搜索
                [ToolCall("s6", "grep", '{"pattern":"value=","path":"src"}')],
                # 7 计算
                [ToolCall("s7", "calc", '{"expression":"2**10"}')],
                # 8 列目录
                [ToolCall("s8", "list_files", '{"path":".","recursive":true}')],
                "链路完成：value=2，2**10=1024。",
            ]
        ),
    )
    try:
        called: list[str] = []
        rt.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(rt, "long-chain")
        try:
            result = handle.followup("按计划重构 src/app.py 并核对。")
        finally:
            handle.dispose()

        assert result.status == "done"
        assert result.steps == 9, f"预期 9 步，实际 {result.steps}"
        assert called == [
            "write_file",
            "write_file",
            "read_file",
            "edit_file",
            "read_file",
            "grep",
            "calc",
            "list_files",
        ]
        assert result.final_text.startswith("链路完成")

        workspace = tmp_path / "ws"
        assert (workspace / "src" / "app.py").read_text(encoding="utf-8") == "value=2\n"
        assert (workspace / "notes.md").is_file()

        messages = rt.ctx.require(SESSIONS_SERVICE).messages("long-chain")
        _assert_tool_calls_are_paired(messages)
        assert sum(1 for m in messages if m.role == "assistant") == 9

        # 搜索与计算的结果真的回到了模型手里（tool 消息内容）
        tool_text = "\n".join(message.text for message in _tool_messages(rt, "long-chain"))
        assert "value=2" in tool_text
        assert "1024" in tool_text
    finally:
        rt.close()


def test_long_chain_recovers_from_a_failed_step(tmp_path: Path) -> None:
    """链路中间一步失败，后续步骤照常继续，且模型能在 tool 消息里看到原因。"""
    rt = _boot(
        tmp_path,
        transport=script_transport(
            [
                [ToolCall("f1", "read_file", '{"path":"missing.txt"}')],
                [ToolCall("f2", "calc", '{"expression":"1+1"}')],
                "第一步失败，已改用计算完成。",
            ]
        ),
    )
    try:
        handle = _agent(rt, "recover")
        try:
            result = handle.followup("读 missing.txt 然后算 1+1。")
        finally:
            handle.dispose()

        assert result.status == "done"
        assert result.steps == 3
        events = rt.ctx.require(SESSIONS_SERVICE).events("recover")
        results = [event.payload for event in events if event.kind == TOOL_RESULT]
        assert [item["status"] for item in results] == ["error", "ok"]
        assert results[0]["message"], "失败结果缺少给模型看的解释"

        messages = rt.ctx.require(SESSIONS_SERVICE).messages("recover")
        assert [m.role for m in messages] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
        ]
        assert messages[3].text.strip() not in ("", "null")
    finally:
        rt.close()


def test_long_chain_honours_the_step_limit(tmp_path: Path) -> None:
    """长链路必须能被 max_steps 截断，且截断后历史仍然自洽。"""
    steps = [
        [ToolCall(f"x{i}", "calc", '{"expression":"1+1"}')] for i in range(10)
    ]
    rt = _boot(
        tmp_path,
        transport=script_transport([*steps, "不该到这里"]),
        loop_config={"default_max_steps": 3},
    )
    try:
        handle = _agent(rt, "limited")
        try:
            result = handle.followup("一直算。")
        finally:
            handle.dispose()

        assert result.status == "max_steps"
        assert result.steps == 3  # quick 模式的 max_steps
        messages = rt.ctx.require(SESSIONS_SERVICE).messages("limited")
        _assert_tool_calls_are_paired(messages)
        assert result.final_text != "不该到这里"
    finally:
        rt.close()


def test_long_chain_with_steer_mid_way(tmp_path: Path) -> None:
    """长链路跑到一半收到 steer：落在下一个 step 边界，不另开一轮。"""
    rt = _boot(
        tmp_path,
        transport=script_transport(
            [
                [ToolCall("a1", "calc", '{"expression":"1+1"}')],
                [ToolCall("a2", "calc", '{"expression":"2+2"}')],
                "收到引导，结束。",
            ]
        ),
    )
    try:
        handle = _agent(rt, "steer-long")
        steered = {"done": False}

        def on_tool_call(call: Any, **_: Any) -> None:
            if not steered["done"]:
                steered["done"] = True
                handle.steer("改用 3+3")

        rt.ctx.on(AGENT_TOOL_CALL, on_tool_call)
        try:
            result = handle.followup("先算 1+1。")
        finally:
            handle.dispose()

        assert result.status == "done"
        events = rt.ctx.require(SESSIONS_SERVICE).events("steer-long")
        user_messages = [
            event for event in events if event.kind == "user/message"
        ]
        assert any(event.payload.get("source") == "steer" for event in user_messages)
        assert sum(1 for event in events if event.kind == "turn/start") == 1
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. 并行会话
# ---------------------------------------------------------------------------


def test_parallel_sessions_keep_logs_and_history_separate(tmp_path: Path) -> None:
    """六个会话各跑五轮：任一崩溃、seq 重复、串台都算失败。"""
    rt = _boot(tmp_path, transport=echo_transport())
    try:
        errors: list[tuple[int, str]] = []
        turns_per_session = 5
        session_count = 6

        def worker(index: int) -> None:
            try:
                handle = _agent(rt, f"p{index}")
                try:
                    for turn in range(turns_per_session):
                        result = handle.followup(f"session-{index}-turn-{turn}")
                        assert result.final_text == f"session-{index}-turn-{turn}"
                finally:
                    handle.dispose()
            except BaseException as exc:  # noqa: BLE001 —— 收集后统一断言
                errors.append((index, f"{type(exc).__name__}: {exc}"))

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(session_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == [], f"并行会话失败：{errors}"

        sessions = rt.ctx.require(SESSIONS_SERVICE)
        assert sorted(sessions.session_ids) == [
            f"p{index}" for index in range(session_count)
        ]
        for index in range(session_count):
            session_id = f"p{index}"
            events = sessions.events(session_id)
            seqs = [event.seq for event in events]
            assert len(seqs) == len(set(seqs)), f"{session_id} 的 seq 有重复"
            assert seqs == sorted(seqs)
            turns = [event for event in events if event.kind == "turn/start"]
            assert len(turns) == turns_per_session

            messages = sessions.messages(session_id)
            for message in messages:
                if message.role in ("user", "assistant"):
                    assert f"session-{index}-" in message.text or message.text == ""

        # 每个会话恰好一个日志文件、恰好一行 header
        logs = list((tmp_path / "sessions").rglob("session.v1.jsonl"))
        assert len(logs) == session_count
        for path in logs:
            lines = path.read_text(encoding="utf-8").splitlines()
            headers = [line for line in lines if '"format"' in line]
            assert len(headers) == 1, f"{path} 有 {len(headers)} 行 header"
            # 1 行 header + session/start + 每轮 5 条事件
            assert len(lines) == 1 + 1 + turns_per_session * 5
    finally:
        rt.close()


def test_concurrent_open_of_the_same_session_converges(tmp_path: Path) -> None:
    """八个入口同时 open 同一条新会话：只建一次，拿到的都是同一个对象。"""
    rt = _boot(tmp_path, transport=echo_transport())
    try:
        sessions = rt.ctx.require(SESSIONS_SERVICE)
        results: list[tuple[bool, Any]] = []
        barrier = threading.Barrier(8)

        def opener() -> None:
            barrier.wait(timeout=5)
            try:
                session, resumed = sessions.open("race")
                results.append((resumed, session))
            except BaseException as exc:  # noqa: BLE001
                results.append((True, exc))

        threads = [threading.Thread(target=opener) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        errors = [value for _, value in results if isinstance(value, BaseException)]
        assert errors == [], f"并发 open 报错：{errors}"
        assert len({id(session) for _, session in results}) == 1, "拿到了多条不同会话"
        assert sum(1 for resumed, _ in results if resumed is False) == 1, (
            "应当恰好有一个入口真正创建了会话"
        )
        starts = [
            event for event in sessions.events("race") if event.kind == "session/start"
        ]
        assert len(starts) == 1, f"写了 {len(starts)} 条 session/start"
    finally:
        rt.close()


def test_concurrent_turns_get_unique_numbers(tmp_path: Path) -> None:
    """十个线程同时开轮：轮次号必须唯一且连续。"""
    rt = _boot(tmp_path, transport=echo_transport())
    try:
        sessions = rt.ctx.require(SESSIONS_SERVICE)
        sessions.open("nums")
        numbers: list[int] = []
        barrier = threading.Barrier(10)

        def starter() -> None:
            barrier.wait(timeout=5)
            numbers.append(sessions.begin_turn("nums", "x"))

        threads = [threading.Thread(target=starter) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(numbers) == list(range(1, 11)), f"轮次号：{sorted(numbers)}"
        events = sessions.events("nums")
        turns = [event.payload["turn"] for event in events if event.kind == "turn/start"]
        assert sorted(turns) == list(range(1, 11))
    finally:
        rt.close()


def test_many_agents_share_one_session_without_losing_turns(tmp_path: Path) -> None:
    """六个 agent 同时跑同一个会话：轮次号唯一，事件 seq 不重复。"""
    rt = _boot(tmp_path, transport=echo_transport())
    try:
        handles = [
            _agent(rt, "shared", agent_id=f"a{index}") for index in range(6)
        ]
        errors: list[str] = []
        barrier = threading.Barrier(len(handles))

        def drive(handle: Any) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(4):
                    assert handle.followup("x").status == "done"
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=drive, args=(handle,)) for handle in handles]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for handle in handles:
            handle.dispose()

        assert errors == [], f"同会话并发失败：{errors}"
        events = rt.ctx.require(SESSIONS_SERVICE).events("shared")
        seqs = [event.seq for event in events]
        assert len(seqs) == len(set(seqs))
        turns = [event.payload["turn"] for event in events if event.kind == "turn/start"]
        assert sorted(turns) == list(range(1, 6 * 4 + 1)), (
            f"轮次号不连续/重复：{sorted(turns)}"
        )
    finally:
        rt.close()


def test_parallel_sessions_can_edit_the_same_workspace_without_losing_writes(
    tmp_path: Path,
) -> None:
    """并行会话各自写不同文件：互不覆盖，最终全部落盘。"""
    rt = _boot(tmp_path, transport=echo_transport())
    try:
        errors: list[str] = []

        def worker(index: int) -> None:
            try:
                tools = rt.ctx.require(TOOL_ADMIN_SERVICE)
                for turn in range(3):
                    result = tools.execute(
                        "write_file",
                        {
                            "path": f"out/s{index}-{turn}.txt",
                            "content": f"{index}:{turn}",
                        },
                        session_id=f"w{index}",
                    )
                    assert result.status == "ok", result.message
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        out = tmp_path / "ws" / "out"
        assert sorted(path.name for path in out.iterdir()) == sorted(
            f"s{index}-{turn}.txt" for index in range(5) for turn in range(3)
        )
        for index in range(5):
            for turn in range(3):
                assert (out / f"s{index}-{turn}.txt").read_text(encoding="utf-8") == (
                    f"{index}:{turn}"
                )
    finally:
        rt.close()


def test_parallel_sessions_each_have_their_own_observation_bucket(
    tmp_path: Path,
) -> None:
    """读后写的记录按会话分桶：A 读过不等于 B 读过。"""
    rt = _boot(tmp_path, transport=echo_transport(), sandbox="changes")
    try:
        workspace = tmp_path / "ws"
        (workspace / "shared.txt").write_text("original\n", encoding="utf-8")
        tools = rt.ctx.require(TOOL_ADMIN_SERVICE)

        assert tools.execute(
            "read_file", {"path": "shared.txt"}, session_id="A"
        ).status == "ok"
        assert tools.execute(
            "write_file",
            {"path": "shared.txt", "content": "by-b"},
            session_id="B",
        ).status == "error"

        tracker = rt.ctx.require("fs-observation-policy")
        assert set(tracker.snapshot()) == {"A"}
        assert (workspace / "shared.txt").read_text(encoding="utf-8") == "original\n"
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 3. 会话内工作区编辑
# ---------------------------------------------------------------------------


def test_session_scoped_read_then_edit_then_verify(tmp_path: Path) -> None:
    """读 → 改 → 读，内容真的变了，且第二次读拿到的是新内容。"""
    rt = _boot(tmp_path, transport=script_transport(
        [
            [ToolCall("e1", "read_file", '{"path":"doc.txt"}')],
            [
                ToolCall(
                    "e2",
                    "edit_file",
                    '{"path":"doc.txt","old":"alpha","new":"beta"}',
                )
            ],
            [ToolCall("e3", "read_file", '{"path":"doc.txt"}')],
            "完成",
        ]
    ), sandbox="changes")
    try:
        workspace = tmp_path / "ws"
        (workspace / "doc.txt").write_text("alpha\nsecond\n", encoding="utf-8")

        handle = _agent(rt, "edit-flow")
        try:
            result = handle.followup("把 doc.txt 里的 alpha 改成 beta。")
        finally:
            handle.dispose()

        assert result.status == "done"
        assert (workspace / "doc.txt").read_text(encoding="utf-8") == "beta\nsecond\n"

        tool_messages = _tool_messages(rt, "edit-flow")
        assert "alpha" in tool_messages[0].text
        assert tool_messages[1].text  # edit 返回替换计数
        assert "beta" in tool_messages[2].text  # 第二次读拿到新内容
        assert (workspace / "doc.txt").read_text(encoding="utf-8") == "beta\nsecond\n"
        _assert_tool_calls_are_paired(
            rt.ctx.require(SESSIONS_SERVICE).messages("edit-flow")
        )
    finally:
        rt.close()


def test_editing_without_reading_is_rejected_per_session(tmp_path: Path) -> None:
    rt = _boot(tmp_path, transport=echo_transport(), sandbox="changes")
    try:
        workspace = tmp_path / "ws"
        (workspace / "doc.txt").write_text("alpha\n", encoding="utf-8")
        tools = rt.ctx.require(TOOL_ADMIN_SERVICE)

        denied = tools.execute(
            "edit_file",
            {"path": "doc.txt", "old": "alpha", "new": "beta"},
            session_id="never-read",
        )
        assert denied.status == "error"
        assert "必须先读" in denied.message
        assert (workspace / "doc.txt").read_text(encoding="utf-8") == "alpha\n"

        assert tools.execute(
            "read_file", {"path": "doc.txt"}, session_id="never-read"
        ).status == "ok"
        allowed = tools.execute(
            "edit_file",
            {"path": "doc.txt", "old": "alpha", "new": "beta"},
            session_id="never-read",
        )
        assert allowed.status == "ok", allowed.message
        assert (workspace / "doc.txt").read_text(encoding="utf-8") == "beta\n"
    finally:
        rt.close()


def test_new_file_can_be_created_without_reading(tmp_path: Path) -> None:
    rt = _boot(tmp_path, transport=echo_transport(), sandbox="changes")
    try:
        tools = rt.ctx.require(TOOL_ADMIN_SERVICE)
        result = tools.execute(
            "write_file",
            {"path": "fresh.txt", "content": "new"},
            session_id="creator",
        )
        assert result.status == "ok", result.message
        assert (tmp_path / "ws" / "fresh.txt").read_text(encoding="utf-8") == "new"
    finally:
        rt.close()


def test_risky_mode_approval_is_deduplicated_per_session(tmp_path: Path) -> None:
    """risky 模式：同一会话同一请求只问一次；换个会话必须重新问。"""
    asked: list[tuple[str | None, str]] = []

    class RecordingApproval:
        def ask(self, request: Any) -> bool:
            asked.append((request.session_id, request.operation))
            return True

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "r.txt").write_text("alpha\n", encoding="utf-8")

    from plugins.base.fs import fs_plugin
    from plugins.base.tools_admin import tools_admin_plugin
    from plugins.base.workspace import workspace_plugin
    from plugins.capability.tools.fs import tool_fs_plugin

    approval = RecordingApproval()
    ctx = Context()
    ctx.load_plugin(workspace_plugin(root=workspace))
    ctx.load_plugin(fs_plugin())
    ctx.load_plugin(tools_admin_plugin(approval=approval))
    ctx.load_plugin(fs_sandbox_plugin(mode="risky", approval=approval))
    ctx.load_plugin(fs_observation_policy_plugin())
    ctx.load_plugin(tool_fs_plugin())
    tools = ctx.require(TOOL_ADMIN_SERVICE)

    # A：读 → 写（问一次）→ 再写（不再问）
    assert tools.execute("read_file", {"path": "r.txt"}, session_id="A").status == "ok"
    assert tools.execute(
        "write_file", {"path": "r.txt", "content": "a1"}, session_id="A"
    ).status == "ok"
    asked_after_first = len(asked)
    assert tools.execute(
        "write_file", {"path": "r.txt", "content": "a2"}, session_id="A"
    ).status == "ok"
    assert len(asked) == asked_after_first, "同一会话的相同请求被重复询问"

    # B：必须重新问（读后写的观察记录也是按会话的）
    assert tools.execute("read_file", {"path": "r.txt"}, session_id="B").status == "ok"
    assert tools.execute(
        "write_file", {"path": "r.txt", "content": "b1"}, session_id="B"
    ).status == "ok"
    assert len(asked) == asked_after_first + 1
    assert {entry[0] for entry in asked} == {"A", "B"}


def test_edit_count_and_repeated_occurrences(tmp_path: Path) -> None:
    """count 语义：默认替换一处，0 表示全部。"""
    rt = _boot(tmp_path, transport=echo_transport(), sandbox="free")
    try:
        workspace = tmp_path / "ws"
        (workspace / "many.txt").write_text("x\nx\nx\n", encoding="utf-8")
        tools = rt.ctx.require(TOOL_ADMIN_SERVICE)
        # 读后写策略在 free 模式下也生效（它管的是"改前必须看"，不是审批）。
        assert tools.execute(
            "read_file", {"path": "many.txt"}, session_id="cnt"
        ).status == "ok"

        one = tools.execute(
            "edit_file",
            {"path": "many.txt", "old": "x", "new": "y"},
            session_id="cnt",
        )
        assert one.status == "ok", one.message
        assert one.content["replacements"] == 1
        assert (workspace / "many.txt").read_text(encoding="utf-8") == "y\nx\nx\n"

        all_ = tools.execute(
            "edit_file",
            {"path": "many.txt", "old": "x", "new": "y", "count": 0},
            session_id="cnt",
        )
        assert all_.status == "ok", all_.message
        assert all_.content["replacements"] == 2
        assert (workspace / "many.txt").read_text(encoding="utf-8") == "y\ny\ny\n"
    finally:
        rt.close()
