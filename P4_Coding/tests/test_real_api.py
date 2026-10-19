"""真实 API 功能验收：每条用例对应一个"功能目标"，用 DeepSeek 实跑。

**默认不运行**（保持离线门禁）：

    python -m pytest tests -q                 # 只跑离线用例（本文件全部 skipped）
    python -m pytest tests/test_real_api.py --real -q   # 真实 API，联网 + 计费

凭证从项目根 `.env` 读（`DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`），也可直接用
环境变量。这些用例断言的是**产品行为**，不是实现细节——失败即说明该功能目标
在真实模型上没有达成。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from core import Context, boot
from manifests.realtime import catalog as real_catalog
from manifests.realtime import manifest as real_manifest
from plugins.base.agent import (
    AGENT_DELTA,
    AGENT_REQUEST,
    AGENT_TOOL_CALL,
    AGENT_TOOL_RESULT,
)
from plugins.base.llm import LLM_SERVICE, LLMAuthError, LLMConnectionError, llm_plugin
from plugins.base.project import PROJECT_SERVICE
from plugins.capability.llm.provider import http_transport, llm_provider_plugin
from plugins.capability.llm.retry import llm_retry_plugin
from plugins.capability.llm.tokens_usages import llm_tokens_usages_plugin
from plugins.capability.prompt.character_admin import character_admin_plugin
from plugins.capability.prompt.system_prompt_admin import system_prompt_admin_plugin

pytestmark = pytest.mark.real

HERE = Path(__file__).resolve().parents[1]
PROJECT_ROOT = HERE.parent


# ---------------------------------------------------------------------------
# 环境与装配
# ---------------------------------------------------------------------------


def _load_env(path: Path = PROJECT_ROOT / ".env") -> None:
    """把项目根 .env 读进环境变量（已存在的优先）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@pytest.fixture(scope="session", autouse=True)
def _real_env() -> None:
    _load_env()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        pytest.skip("没有 DEEPSEEK_API_KEY：在项目根 .env 或环境变量里配置")


def _model() -> str:
    return os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")


def _boot(
    workspace: Path,
    sessions: Path,
    *,
    prompt_sections: list[dict[str, Any]] | None = None,
    extra_plugins: dict[str, Any] | None = None,
    approval_policy: str = "allow",
):
    workspace.mkdir(parents=True, exist_ok=True)
    entries = real_manifest(
        workspace=workspace,
        sessions=sessions,
        sandbox_mode="free",
        approval_policy=approval_policy,
    )
    if prompt_sections:
        for entry in entries[0]["children"]:
            if entry["id"] == "prompt":
                entry["config"]["sections"].extend(prompt_sections)
    plugin_catalog = real_catalog()
    for name, factory in (extra_plugins or {}).items():
        plugin_catalog[name] = factory
        entries[0]["children"].append(
            {"id": name, "name": name, "role": "capability"}
        )
    return boot(entries, catalog=plugin_catalog)


@contextmanager
def real_runtime(
    tmp_path: Path,
    *,
    prompt_sections: list[dict[str, Any]] | None = None,
    extra_plugins: dict[str, Any] | None = None,
    approval_policy: str = "allow",
) -> Iterator[tuple[Any, Path, Path]]:
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    runtime = _boot(
        workspace,
        sessions,
        prompt_sections=prompt_sections,
        extra_plugins=extra_plugins,
        approval_policy=approval_policy,
    )
    try:
        yield runtime, workspace, sessions
    finally:
        runtime.close()


def _agent(runtime: Any, session_id: str, **options: Any):
    return runtime.ctx.require(PROJECT_SERVICE).start(session_id=session_id, **options)


def _stream_text(chunks: list[Any]) -> str:
    return "".join(
        chunk.text for chunk in chunks if getattr(chunk, "type", None) == "text"
    )


# ---------------------------------------------------------------------------
# 目标 1：装配——真实模型装配单能把整套插件树装起来
# ---------------------------------------------------------------------------


def test_goal_real_assembly_boots_complete_tree(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        states = runtime.snapshot()
        assert states, "装配单不应为空"
        not_running = [
            (state.path, state.status, state.error)
            for state in states
            if state.status not in ("active", "group")
        ]
        assert not_running == [], f"有插件没起来：{not_running}"
        assert len(runtime.ctx.active_plugins) >= 20


# ---------------------------------------------------------------------------
# 目标 2：LLM 接缝——真实流式调用、结束标记、用量统计
# ---------------------------------------------------------------------------


def test_goal_llm_streams_text_and_reports_usage(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        llm = runtime.ctx.require(LLM_SERVICE)
        request = llm.request(
            [{"role": "user", "content": "请直接回答：4+4 等于几？只给数字。"}],
            max_tokens=2048,
        )
        chunks = list(llm.stream(request))
        text = _stream_text(chunks).strip()
        finishes = [c for c in chunks if getattr(c, "type", None) == "finish"]

        assert text, "真实模型没有返回任何文本"
        assert "8" in text
        assert len(finishes) == 1, "一次调用必须恰好一个 Finish"
        assert finishes[0].reason == "stop"
        assert finishes[0].usage is not None
        assert finishes[0].usage.total_tokens > 0


# ---------------------------------------------------------------------------
# 目标 3：提示词装配——渲染出的系统提示词要真的发给模型
# ---------------------------------------------------------------------------


def test_goal_system_prompt_reaches_model(tmp_path: Path) -> None:
    sections = [
        {"name": "codename", "order": 20, "text": "你的内部代号是 ORION-42。"}
    ]
    with real_runtime(tmp_path, prompt_sections=sections) as (runtime, _ws, _sessions):
        requests: list[Any] = []
        runtime.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(runtime, "prompt-real")
        try:
            handle.followup("请回答：1+1 等于几？只给数字。")
        finally:
            handle.dispose()

        assert requests, "没有观察到模型请求"
        system_messages = [
            message for message in requests[0].messages if message.role == "system"
        ]
        assert system_messages, "发给模型的 messages 里没有 system 消息"
        assert "ORION-42" in "".join(message.text for message in system_messages)


def test_goal_model_obeys_system_prompt(tmp_path: Path) -> None:
    sections = [
        {"name": "codename", "order": 20, "text": "你的内部代号是 ORION-42。"}
    ]
    with real_runtime(tmp_path, prompt_sections=sections) as (runtime, _ws, _sessions):
        handle = _agent(runtime, "prompt-behavior-real")
        try:
            result = handle.followup("系统提示词里给你的内部代号是什么？只回答代号本身。")
        finally:
            handle.dispose()
        assert "ORION-42" in result.final_text, (
            f"模型没有按系统提示词回答，实际：{result.final_text!r}"
        )


# ---------------------------------------------------------------------------
# 目标 4：工具调用——模型自主调用工具并拿到结果
# ---------------------------------------------------------------------------


def test_goal_model_calls_calc_tool(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        called: list[str] = []
        runtime.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(runtime, "calc-real")
        try:
            result = handle.followup(
                "请必须调用 calc 工具计算 123*456，然后在回答里给出最终数字。"
            )
        finally:
            handle.dispose()

        assert "calc" in called, f"模型没有调用 calc（实际调用：{called}）"
        assert "56088" in result.final_text
        assert result.status == "done"


def test_goal_model_uses_file_tools(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        called: list[str] = []
        runtime.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(runtime, "fs-real")
        try:
            result = handle.followup(
                "请用 write_file 工具把文本 p4-real-ok 写到 notes.txt（内容只写 "
                "p4-real-ok），然后用 read_file 读回来确认。"
            )
        finally:
            handle.dispose()

        assert {"write_file", "read_file"} <= set(called), f"实际调用：{called}"
        assert (workspace / "notes.txt").is_file()
        assert (workspace / "notes.txt").read_text(encoding="utf-8").strip() == "p4-real-ok"
        assert result.status == "done"


def test_goal_model_uses_shell_tool(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        called: list[str] = []
        runtime.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(runtime, "shell-real")
        try:
            result = handle.followup(
                "请用 shell 工具执行命令 `echo p4-shell-ok`，然后告诉我它的输出。"
            )
        finally:
            handle.dispose()

        assert "shell" in called, f"模型没有调用 shell（实际调用：{called}）"
        assert "p4-shell-ok" in result.final_text


def test_goal_workspace_jail_blocks_escape(tmp_path: Path) -> None:
    """越界拦截是运行时的硬边界，不依赖模型是否愿意照做。

    真实模型常常拒绝写入 `../`（这本身是好事），所以这里直接走工具面：
    同一个真实装配、同一条 `tool-admin -> fs -> workspace.resolve` 路径。
    """
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        tools = runtime.ctx.require("tool-admin")
        result = tools.execute(
            "write_file",
            {"path": "../escape.txt", "content": "x"},
            session_id="jail-real",
        )

        assert not (workspace.parent / "escape.txt").exists(), "越界文件被写出来了"
        assert result.status in ("denied", "error"), (
            f"越界写入没有被拒绝：status={result.status} message={result.message}"
        )
        assert "越界" in result.message

        inside = tools.execute(
            "write_file",
            {"path": "inside.txt", "content": "ok"},
            session_id="jail-real",
        )
        assert inside.status == "ok", inside.message
        assert (workspace / "inside.txt").read_text(encoding="utf-8") == "ok"


# ---------------------------------------------------------------------------
# 目标 5：会话——多轮记忆、落盘与重启后恢复
# ---------------------------------------------------------------------------


def test_goal_multi_turn_memory(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        handle = _agent(runtime, "memory-real")
        try:
            handle.followup("请记住数字 7391。只回复 OK。")
            result = handle.followup("我刚才让你记住的数字是多少？只回答数字。")
        finally:
            handle.dispose()
        assert "7391" in result.final_text, f"多轮历史没有生效：{result.final_text!r}"


def test_goal_session_survives_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"

    first = _boot(workspace, sessions)
    try:
        handle = _agent(first, "persist-real")
        try:
            handle.followup("请记住数字 7391。只回复 OK。")
        finally:
            handle.dispose()
    finally:
        first.close()

    second = _boot(workspace, sessions)
    try:
        handle = _agent(second, "persist-real")
        try:
            result = handle.followup("我刚才让你记住的数字是多少？只回答数字。")
        finally:
            handle.dispose()
    finally:
        second.close()

    assert "7391" in result.final_text, (
        f"重启后会话历史没有恢复：{result.final_text!r}"
    )
    logs = list(sessions.rglob("session.v1.jsonl"))
    assert logs, "会话日志没有落盘"


# ---------------------------------------------------------------------------
# 目标 6：能力插件——token 统计、重试策略、agent 模式
# ---------------------------------------------------------------------------


def test_goal_token_usage_is_recorded(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        runtime.ctx.load_plugin(llm_tokens_usages_plugin())
        handle = _agent(runtime, "usage-real")
        try:
            handle.followup("只回复 OK。")
        finally:
            handle.dispose()

        snapshot = runtime.ctx.require("llm-tokens-usages").snapshot()
        assert snapshot["totals"]["calls"] >= 1
        assert snapshot["totals"]["total_tokens"] > 0
        assert "usage-real" in snapshot["sessions"]


def test_goal_retry_recovers_transient_failure(tmp_path: Path) -> None:
    """第一次传输失败、第二次成功：重试插件应透明恢复，且不重复文本。"""
    attempts = {"n": 0}

    def flaky_transport(url: str, headers: Any, payload: Any, timeout: float):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise LLMConnectionError("模拟连接中断")
        return http_transport(url, headers, payload, timeout)

    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="deepseek", default_model=_model()))
    ctx.load_plugin(
        llm_provider_plugin(
            route="deepseek",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
            models=[
                {
                    "id": _model(),
                    "context_window": 128_000,
                    "max_output_tokens": 8_192,
                }
            ],
            transport=flaky_transport,
        )
    )
    ctx.load_plugin(llm_retry_plugin(max_attempts=3, backoff_seconds=0))

    llm = ctx.require(LLM_SERVICE)
    chunks = list(
        llm.stream(
            llm.request(
                [{"role": "user", "content": "请只回复 OK。"}], max_tokens=2048
            )
        )
    )
    assert attempts["n"] == 2, f"重试次数不对：{attempts['n']}"
    assert _stream_text(chunks).strip()


def test_goal_quick_mode_caps_steps(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        handle = _agent(runtime, "mode-real", mode="quick")
        try:
            result = handle.followup("1+1 等于几？只给数字。")
        finally:
            handle.dispose()
        assert result.status == "done"
        assert result.steps <= 3, f"quick 模式没有限步：steps={result.steps}"


# ---------------------------------------------------------------------------
# 目标 7：产品入口——app.py 的真实模型 / YAML 两条路径
# ---------------------------------------------------------------------------


def _run_app(args: list[str], timeout: float = 240.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "app.py", *args],
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_goal_app_cli_real_model(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    result = _run_app(
        [
            "--realtime",
            "--yes",
            "--sandbox",
            "free",
            "--ask",
            "只回复 CLI-OK",
            "--session",
            "cli-real",
            "--workspace",
            str(workspace),
            "--sessions",
            str(sessions),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "[done]" in result.stdout
    assert list(sessions.rglob("session.v1.jsonl")), "CLI 没有写会话日志"


def test_goal_yaml_manifest_real_route(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    result = _run_app(
        [
            "--manifest",
            "manifests/minimal.yaml",
            "--provider",
            "deepseek",
            "--model",
            _model(),
            "--yes",
            "--sandbox",
            "free",
            "--ask",
            "只回复 YAML-OK",
            "--session",
            "yaml-real",
            "--workspace",
            str(workspace),
            "--sessions",
            str(sessions),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "[done]" in result.stdout


# ---------------------------------------------------------------------------
# 目标 8：边界语义——取消保留已生成文本、预算超限不留悬空工具调用
# ---------------------------------------------------------------------------


def test_goal_cancel_keeps_partial_text(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        handle = _agent(runtime, "cancel-real")
        started = threading.Event()

        def on_delta(text: str, **_: Any) -> None:
            if text and not started.is_set():
                started.set()
                handle.cancel()

        runtime.ctx.on(AGENT_DELTA, on_delta)
        try:
            result = handle.followup("请用至少 50 个字介绍你自己。")
        finally:
            handle.dispose()

        assert started.is_set()
        assert result.status == "cancelled"
        messages = runtime.ctx.require("sessions").messages("cancel-real")
        assistant = [message for message in messages if message.role == "assistant"]
        assert assistant and assistant[-1].text.strip(), (
            "取消后已生成的文本应保留在会话里（llm.py/loop.py 的文档承诺）"
        )


def test_goal_budget_exceeded_leaves_no_dangling_tool_calls(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        called: list[str] = []
        runtime.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(runtime, "budget-real", token_budget=1)
        try:
            result = handle.followup("请必须调用 calc 工具计算 123*456。")
        finally:
            handle.dispose()

        assert result.status == "budget_exceeded"
        messages = runtime.ctx.require("sessions").messages("budget-real")
        for index, message in enumerate(messages):
            if message.role == "assistant" and message.tool_calls:
                has_result = any(
                    later.role == "tool" for later in messages[index + 1 :]
                )
                assert has_result, (
                    "assistant 消息带 tool_calls，但历史里没有配套的 tool 结果；"
                    "下一轮请求会被 OpenAI 兼容接口拒绝"
                )


# ---------------------------------------------------------------------------
# 目标 9：审批——拒绝时工具不执行（fail-closed）
# ---------------------------------------------------------------------------


def test_goal_approval_denial_blocks_tool(tmp_path: Path) -> None:
    with real_runtime(tmp_path, approval_policy="deny") as (runtime, workspace, _):
        tools = runtime.ctx.require("tool-admin")
        result = tools.execute(
            "shell", {"command": "echo should-not-run"}, session_id="deny-real"
        )
        assert result.status == "denied", result.message
        assert not (workspace / "should-not-run").exists()


# ---------------------------------------------------------------------------
# 目标 10：会话隔离——不同会话的记忆互不串台
# ---------------------------------------------------------------------------


def test_goal_sessions_are_isolated(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        first = _agent(runtime, "iso-a")
        try:
            first.followup("请记住数字 7391。只回复 OK。")
        finally:
            first.dispose()

        second = _agent(runtime, "iso-b")
        try:
            other = second.followup(
                "我刚才让你记住的数字是多少？如果你没有收到过任何数字，只回答：不知道。"
            )
        finally:
            second.dispose()
        assert "7391" not in other.final_text, (
            f"会话之间串台了：{other.final_text!r}"
        )

        again = _agent(runtime, "iso-a")
        try:
            remembered = again.followup("请说出我让你记住的数字。只回答数字。")
        finally:
            again.dispose()
        assert "7391" in remembered.final_text


# ---------------------------------------------------------------------------
# 目标 11：提示词插件——角色与管理员 override 都进入模型请求
# ---------------------------------------------------------------------------


def test_goal_character_reaches_model(tmp_path: Path) -> None:
    with real_runtime(
        tmp_path, extra_plugins={"character-admin": character_admin_plugin}
    ) as (runtime, _ws, _sessions):
        runtime.ctx.require("character-admin").set("char-real", "reviewer")
        requests: list[Any] = []
        runtime.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(runtime, "char-real")
        try:
            handle.followup("只回复 OK。")
        finally:
            handle.dispose()

        system = "".join(
            message.text for message in requests[0].messages if message.role == "system"
        )
        assert "代码审查者" in system


def test_goal_system_prompt_override_reaches_model(tmp_path: Path) -> None:
    with real_runtime(
        tmp_path, extra_plugins={"system-prompt-admin": system_prompt_admin_plugin}
    ) as (runtime, _ws, _sessions):
        runtime.ctx.require("system-prompt-admin").override(
            "override-real", "附加规则：回答末尾必须加上 [OV]。"
        )
        requests: list[Any] = []
        runtime.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(runtime, "override-real")
        try:
            handle.followup("只回复 OK。")
        finally:
            handle.dispose()

        system = "".join(
            message.text for message in requests[0].messages if message.role == "system"
        )
        assert "[OV]" in system


def test_goal_prompt_change_mid_session_is_recorded(tmp_path: Path) -> None:
    with real_runtime(
        tmp_path, extra_plugins={"character-admin": character_admin_plugin}
    ) as (runtime, _ws, _sessions):
        requests: list[Any] = []
        runtime.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(runtime, "prompt-change-real")
        try:
            handle.followup("只回复 OK。")
            runtime.ctx.require("character-admin").set("prompt-change-real", "reviewer")
            handle.followup("只回复 OK。")
        finally:
            handle.dispose()

        assert len(requests) >= 2
        before = "".join(
            message.text for message in requests[0].messages if message.role == "system"
        )
        after = "".join(
            message.text for message in requests[-1].messages if message.role == "system"
        )
        assert "代码审查者" not in before
        assert "代码审查者" in after

        events = runtime.ctx.require("sessions").events("prompt-change-real")
        assert any(event.kind == "system/message" for event in events), (
            "提示词变了，但会话日志里没有追加 system/message"
        )


# ---------------------------------------------------------------------------
# 目标 12：中途注入——inject 的上下文在下一轮可见
# ---------------------------------------------------------------------------


def test_goal_injected_context_is_seen_by_model(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        handle = _agent(runtime, "inject-real")
        try:
            handle.inject("内部资料：项目代号是 NEBULA-7。")
            result = handle.followup(
                "我给你的内部资料里的项目代号是什么？只回答代号。"
            )
        finally:
            handle.dispose()
        assert "NEBULA-7" in result.final_text


# ---------------------------------------------------------------------------
# 目标 13：工具开关——停用的工具对模型不可见
# ---------------------------------------------------------------------------


def test_goal_disabled_tool_is_hidden_from_the_model(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        tools = runtime.ctx.require("tool-admin")
        requests: list[Any] = []
        runtime.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(runtime, "toggle-real")
        try:
            before = len(requests)
            handle.followup("只回复 OK。")
            names_before = {spec.name for spec in requests[before].tools}
            assert "calc" in names_before

            tools.disable("calc")
            before = len(requests)
            handle.followup("只回复 OK。")
            names_disabled = {spec.name for spec in requests[before].tools}
            assert "calc" not in names_disabled

            tools.enable("calc")
            before = len(requests)
            handle.followup("只回复 OK。")
            names_restored = {spec.name for spec in requests[before].tools}
            assert "calc" in names_restored
        finally:
            handle.dispose()


# ---------------------------------------------------------------------------
# 目标 14：一次 turn 里串联多件工具
# ---------------------------------------------------------------------------


def test_goal_model_chains_two_tools_in_one_turn(tmp_path: Path) -> None:
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        called: list[str] = []
        runtime.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(runtime, "chain-real")
        try:
            result = handle.followup(
                "请按顺序做两件事：1) 必须调用 calc 计算 6*7；2) 必须调用 write_file "
                "把计算结果写到 answer.txt，内容只写数字。"
            )
        finally:
            handle.dispose()

        assert {"calc", "write_file"} <= set(called), f"实际调用：{called}"
        assert result.status == "done"
        assert (workspace / "answer.txt").is_file()
        assert "42" in (workspace / "answer.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 目标 15：凭据无效必须 fail loud（不能静默变成空回答）
# ---------------------------------------------------------------------------


def test_goal_invalid_credentials_fail_loud(tmp_path: Path) -> None:
    ctx = Context()
    ctx.load_plugin(llm_plugin(default_provider="bad", default_model=_model()))
    ctx.load_plugin(
        llm_provider_plugin(
            route="bad",
            base_url="https://api.deepseek.com",
            api_key="sk-invalid-for-test",
            models=[
                {
                    "id": _model(),
                    "context_window": 128_000,
                    "max_output_tokens": 8_192,
                }
            ],
            timeout=30,
        )
    )
    llm = ctx.require(LLM_SERVICE)
    with pytest.raises(LLMAuthError):
        list(
            llm.stream(
                llm.request(
                    [{"role": "user", "content": "只回复 OK。"}], max_tokens=2048
                )
            )
        )


# ---------------------------------------------------------------------------
# 目标 16：长链路——真实模型连续多步工具调用（读 → 改 → 验 → 跑）
# ---------------------------------------------------------------------------


def test_goal_long_chain_read_edit_verify(tmp_path: Path) -> None:
    """一次任务里连续 5+ 步：读文件 → 改文件 → 再读 → 跑命令 → 总结。

    这是 P4 的 dogfood 验收入口：不是"能调一次工具"，而是"能按计划连续干完
    一件事"。断言最终**磁盘状态**与工具调用序列，不依赖模型的具体措辞。
    """
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        (workspace / "app.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8"
        )
        called: list[str] = []
        runtime.ctx.on(AGENT_TOOL_CALL, lambda call, **_: called.append(call.name))

        handle = _agent(runtime, "long-real")
        try:
            result = handle.followup(
                "工作区里的 app.py 有一个 bug：add 函数应该做加法。"
                "请按这个顺序完成，每步都要真的调用工具："
                "1) 用 read_file 读 app.py；"
                "2) 用 edit_file 把 a - b 改成 a + b；"
                "3) 用 read_file 再读一次确认；"
                "4) 用 shell 运行 `python -c \"import app; print(app.add(2,3))\"` 验证输出 5；"
                "5) 最后汇报结果。"
            )
        finally:
            handle.dispose()

        assert result.status == "done"
        assert result.steps >= 4, f"步数太少，模型没有按计划分步执行：{result.steps}"
        assert {"read_file", "edit_file", "shell"} <= set(called), f"实际调用：{called}"

        assert (workspace / "app.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a + b\n"
        )
        assert "5" in result.final_text

        messages = runtime.ctx.require("sessions").messages("long-real")
        for index, message in enumerate(messages):
            if message.role == "assistant" and message.tool_calls:
                expected = {call.id for call in message.tool_calls}
                actual = {
                    later.tool_call_id
                    for later in messages[index + 1 :]
                    if later.role == "tool"
                }
                assert expected <= actual, f"长链路里出现悬空 tool_calls：{expected - actual}"


def test_goal_long_chain_survives_a_wrong_guess(tmp_path: Path) -> None:
    """模型第一步猜错文件，看到错误后自行纠正，最终仍完成任务。"""
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        (workspace / "real.txt").write_text("SECRET-9\n", encoding="utf-8")
        tool_results: list[Any] = []
        runtime.ctx.on(
            AGENT_TOOL_RESULT, lambda result, **_: tool_results.append(result)
        )

        handle = _agent(runtime, "recover-real")
        try:
            result = handle.followup(
                "请用 read_file 读 likely.txt；如果它不存在，就改读 real.txt，"
                "然后告诉我 real.txt 里的内容。"
            )
        finally:
            handle.dispose()

        assert result.status == "done"
        assert "SECRET-9" in result.final_text
        assert any(item.status == "error" for item in tool_results), (
            "预期第一次读失败，但没有任何错误结果——模型可能跳过了工具"
        )
        assert any(item.status == "ok" for item in tool_results)


# ---------------------------------------------------------------------------
# 目标 17：并行会话——真实模型下多个会话同时跑，互不串台
# ---------------------------------------------------------------------------


def test_goal_parallel_sessions_with_real_model(tmp_path: Path) -> None:
    """三个会话并行各跑两轮真实调用：各自只记得自己的暗号。"""
    with real_runtime(tmp_path) as (runtime, _ws, session_paths):
        errors: list[str] = []
        tokens = {"alpha": "ALPHA-111", "beta": "BETA-222", "gamma": "GAMMA-333"}

        def worker(session_id: str, token: str) -> None:
            try:
                handle = _agent(runtime, session_id)
                try:
                    handle.followup(f"请记住暗号 {token}。只回复 OK。")
                    answer = handle.followup("我刚才给你的暗号是什么？只回答暗号。")
                finally:
                    handle.dispose()
                if token not in answer.final_text:
                    errors.append(f"{session_id} 没记住自己的暗号：{answer.final_text!r}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{session_id} 崩溃：{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(session_id, token))
            for session_id, token in tokens.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == [], f"并行会话失败：{errors}"

        sessions = runtime.ctx.require("sessions")
        for session_id, token in tokens.items():
            events = sessions.events(session_id)
            seqs = [event.seq for event in events]
            assert len(seqs) == len(set(seqs)), f"{session_id} 的 seq 有重复"
            assert seqs == sorted(seqs)
            text = " ".join(
                message.text
                for message in sessions.messages(session_id)
                if message.role in ("user", "assistant")
            )
            assert token in text
            for other_session, other_token in tokens.items():
                if other_session == session_id:
                    continue
                assert other_token not in text, (
                    f"{session_id} 串到了 {other_session} 的暗号 {other_token}"
                )

        logs = list((tmp_path / "sessions").rglob("session.v1.jsonl"))
        assert len(logs) == len(tokens)
        for path in logs:
            headers = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if '"format"' in line
            ]
            assert len(headers) == 1


# ---------------------------------------------------------------------------
# 目标 18：会话内工作区编辑——读后写约束在真实模型上成立
# ---------------------------------------------------------------------------


def test_goal_edit_requires_reading_first_in_real_session(tmp_path: Path) -> None:
    """真实装配里，未读过的文件不能直接改；读过之后才能改。

    这里不走模型：模型可能"先读再改"（这本身是期望行为），那样就验证不到
    拒绝路径。策略是运行时的硬边界，用工具面直接打，结果确定。
    """
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        (workspace / "guarded.txt").write_text("original\n", encoding="utf-8")
        tools = runtime.ctx.require("tool-admin")

        denied = tools.execute(
            "edit_file",
            {"path": "guarded.txt", "old": "original", "new": "hacked"},
            session_id="guard-real",
        )
        assert denied.status in ("error", "denied"), denied.message
        assert "必须先读" in denied.message
        assert (workspace / "guarded.txt").read_text(encoding="utf-8") == "original\n"

        assert tools.execute(
            "read_file", {"path": "guarded.txt"}, session_id="guard-real"
        ).status == "ok"
        allowed = tools.execute(
            "edit_file",
            {"path": "guarded.txt", "old": "original", "new": "hacked"},
            session_id="guard-real",
        )
        assert allowed.status == "ok", allowed.message
        assert (workspace / "guarded.txt").read_text(encoding="utf-8") == "hacked\n"


def test_goal_session_edits_do_not_leak_across_sessions(tmp_path: Path) -> None:
    """A 会话读过的文件，不能让 B 会话获得写权限。"""
    with real_runtime(tmp_path) as (runtime, workspace, _sessions):
        (workspace / "shared.txt").write_text("original\n", encoding="utf-8")
        tools = runtime.ctx.require("tool-admin")

        assert tools.execute(
            "read_file", {"path": "shared.txt"}, session_id="session-A"
        ).status == "ok"
        denied = tools.execute(
            "write_file",
            {"path": "shared.txt", "content": "by-b"},
            session_id="session-B",
        )
        assert denied.status == "error"
        assert "必须先读" in denied.message
        assert (workspace / "shared.txt").read_text(encoding="utf-8") == "original\n"

        allowed = tools.execute(
            "write_file",
            {"path": "shared.txt", "content": "by-a"},
            session_id="session-A",
        )
        assert allowed.status == "ok", allowed.message
        assert (workspace / "shared.txt").read_text(encoding="utf-8") == "by-a"


# ---------------------------------------------------------------------------
# 目标 19：长链路走产品入口（app.py 一次任务）
# ---------------------------------------------------------------------------


def test_goal_app_cli_completes_a_real_coding_task(tmp_path: Path) -> None:
    """走 app.py 真实入口完成"读代码 → 改 → 跑测试"，不碰内部 API。"""
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "calc.py").write_text(
        "def multiply(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (workspace / "test_calc.py").write_text(
        "from calc import multiply\n\n"
        "def test_multiply():\n"
        "    assert multiply(3, 4) == 12\n",
        encoding="utf-8",
    )

    result = _run_app(
        [
            "--realtime",
            "--yes",
            "--sandbox",
            "free",
            "--ask",
            "工作区里 calc.py 的 multiply 写错了。请用 read_file 读它，"
            "用 edit_file 把 a + b 改成 a * b，然后用 shell 运行 "
            "`python -m pytest test_calc.py -q` 确认通过，最后只回复 PASS 或 FAIL。",
            "--session",
            "cli-task-real",
            "--workspace",
            str(workspace),
            "--sessions",
            str(sessions),
        ],
        timeout=300.0,
    )
    assert result.returncode == 0, result.stderr
    assert (workspace / "calc.py").read_text(encoding="utf-8").endswith(
        "return a * b\n"
    ), f"文件没被改对；stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "PASS" in result.stdout.upper(), result.stdout
    assert list(sessions.rglob("session.v1.jsonl")), "没有写会话日志"


# ---------------------------------------------------------------------------
# 目标 20：多会话并行 + 同会话多 agent（真实模型）
# ---------------------------------------------------------------------------


def test_goal_many_sessions_in_parallel_with_real_model(tmp_path: Path) -> None:
    """五个会话并行各跑两轮：各自的暗号不串台，日志与 seq 都干净。"""
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        tokens = {f"s{index}": f"TOKEN-{index}{index}{index}" for index in range(5)}
        errors: list[str] = []

        def worker(session_id: str, token: str) -> None:
            try:
                handle = _agent(runtime, session_id)
                try:
                    handle.followup(f"请记住 {token}。只回复 OK。")
                    answer = handle.followup("我让你记住的暗号是什么？只回答暗号。")
                finally:
                    handle.dispose()
                if token not in answer.final_text:
                    errors.append(f"{session_id}: {answer.final_text!r}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{session_id} 崩溃：{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(session_id, token))
            for session_id, token in tokens.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == [], f"并行失败：{errors}"

        sessions = runtime.ctx.require("sessions")
        for session_id, token in tokens.items():
            events = sessions.events(session_id)
            seqs = [event.seq for event in events]
            assert len(seqs) == len(set(seqs)), f"{session_id} seq 重复"
            text = " ".join(
                message.text
                for message in sessions.messages(session_id)
                if message.role in ("user", "assistant")
            )
            assert token in text
            for other_id, other_token in tokens.items():
                if other_id != session_id:
                    assert other_token not in text, f"{session_id} 串到 {other_id}"


def test_goal_two_agents_share_one_session_with_real_model(tmp_path: Path) -> None:
    """两个 agent 同时跑同一个会话：轮次号唯一，不互相覆盖历史。"""
    with real_runtime(tmp_path) as (runtime, _ws, _sessions):
        first = _agent(runtime, "shared-real", agent_id="real-a")
        second = _agent(runtime, "shared-real", agent_id="real-b")
        errors: list[str] = []
        barrier = threading.Barrier(2)

        def drive(handle: Any, label: str) -> None:
            try:
                barrier.wait(timeout=10)
                for index in range(2):
                    result = handle.followup(f"{label}-{index}：只回复 OK。")
                    assert result.status == "done"
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{label}: {type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=drive, args=(first, "A")),
            threading.Thread(target=drive, args=(second, "B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        first.dispose()
        second.dispose()

        assert errors == [], f"同会话并发失败：{errors}"
        sessions = runtime.ctx.require("sessions")
        events = sessions.events("shared-real")
        seqs = [event.seq for event in events]
        assert len(seqs) == len(set(seqs))
        turns = [event.payload["turn"] for event in events if event.kind == "turn/start"]
        assert sorted(turns) == [1, 2, 3, 4], f"轮次号异常：{turns}"
