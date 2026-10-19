"""功能目标验收（离线）：把"产品能不能干活"钉成可重复的断言。

与 `test_real_api.py` 的关系：

    test_real_api.py    真实模型（联网、计费、默认跳过）
    本文件              同一批功能目标的**离线**版本：模型走剧本，其余链路全是真的

覆盖的目标（每条对应一个用户可见的功能，而不是某个实现细节）：

    1. 失败的工具结果要能被模型读懂（错误原因进入 tool 消息）
    2. 提示词变化要落进会话日志，并且模型下一轮看得到新版本
    3. 角色 / override 通过真实提示词装配进入请求
    4. 停用工具后模型立即看不到，启用后恢复
    5. 会话隔离：两个会话的记忆互不串台
    6. 热插拔：运行中换模型路由，产品不断档
    7. 产品政策：缺 application 真拒绝、补齐真能起
    8. 一次 turn 里的多步工具链
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core import Context, PolicyError, boot
from manifests.minimal import catalog, manifest
from plugins.base.agent import AGENT_REQUEST, AGENT_TOOL_CALL
from plugins.base.llm import LLM_SERVICE, LLMRequest, ToolCall
from plugins.base.project import PROJECT_SERVICE
from plugins.base.sessions import ASSISTANT, SESSIONS_SERVICE, SYSTEM_MESSAGE, TOOL_RESULT
from plugins.base.tools_admin import TOOL_ADMIN_SERVICE
from plugins.capability.prompt.character_admin import character_admin_plugin
from plugins.capability.prompt.system_prompt_admin import system_prompt_admin_plugin

# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def _boot(
    tmp_path: Path,
    *,
    replies: list[str] | None = None,
    extra_plugins: dict[str, Any] | None = None,
    catalog_extras: dict[str, Any] | None = None,
) -> Any:
    workspace = tmp_path / "ws"
    sessions = tmp_path / "sessions"
    workspace.mkdir(parents=True, exist_ok=True)
    entries = manifest(
        workspace=workspace,
        sessions=sessions,
        sandbox_mode="free",
        approval_policy="allow",
        replies=replies or ["好的"],
    )
    plugin_catalog = catalog()
    for name, factory in (extra_plugins or {}).items():
        plugin_catalog[name] = factory
        entries[0]["children"].append(
            {"id": name, "name": name, "role": "capability"}
        )
    # 只进白名单、不进装配单：用于验证"运行中热插拔装载"。
    plugin_catalog.update(catalog_extras or {})
    return boot(entries, catalog=plugin_catalog)


@pytest.fixture
def runtime(tmp_path: Path):
    rt = _boot(tmp_path)
    try:
        yield rt
    finally:
        rt.close()


def _agent(rt: Any, session_id: str, **options: Any):
    return rt.ctx.require(PROJECT_SERVICE).start(session_id=session_id, **options)


# ---------------------------------------------------------------------------
# 1. 失败的工具结果要能被模型读懂
# ---------------------------------------------------------------------------


def test_failed_tool_result_reaches_the_model_with_its_reason(tmp_path: Path) -> None:
    """工具失败时 content 为 None，解释在 message 里。

    只投影 content 会让模型看到字符串 "null"——等于没拿到失败原因，
    下一 step 只能瞎猜。这里用一个必然越界的写文件来制造失败。
    """
    rt = _boot(tmp_path)
    try:
        _swap_transport(
            rt,
            _script_transport(
                [
                    [ToolCall("w1", "write_file", '{"path":"../escape.txt","content":"x"}')],
                    "写入失败了。",
                ]
            ),
        )
        handle = _agent(rt, "err-tool")
        try:
            result = handle.followup("请调用 write_file 把 x 写到 ../escape.txt。")
        finally:
            handle.dispose()

        messages = rt.ctx.require(SESSIONS_SERVICE).messages("err-tool")
        tool_messages = [message for message in messages if message.role == "tool"]
        assert tool_messages, f"没有工具结果被投影：{result.status}"
        text = tool_messages[-1].text
        assert text.strip() and text.strip() != "null"
        assert "越界" in text or "denied" in text or "error" in text.lower()
    finally:
        rt.close()


def test_tool_exception_is_reported_not_raised(tmp_path: Path) -> None:
    """工具内部抛异常 → 一次失败结果，turn 不崩。"""
    rt = _boot(tmp_path)
    try:
        _swap_transport(
            rt,
            _script_transport(
                [
                    [ToolCall("z1", "calc", '{"expression":"1/0"}')],
                    "计算失败了。",
                ]
            ),
        )
        handle = _agent(rt, "boom")
        try:
            result = handle.followup("请调用 calc 计算 1/0。")
        finally:
            handle.dispose()
        assert result.status == "done", "工具失败不应让 turn 变成 error"
        events = rt.ctx.require(SESSIONS_SERVICE).events("boom")
        results = [e for e in events if e.kind == TOOL_RESULT]
        assert results, "没有落任何 tool/result"
        assert results[-1].payload["status"] in ("error", "denied", "timeout")
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2/3. 提示词：角色、override、变更落日志
# ---------------------------------------------------------------------------


def _system_text(request: LLMRequest) -> str:
    return "".join(
        message.text for message in request.messages if message.role == "system"
    )


def test_character_admin_reaches_the_request(tmp_path: Path) -> None:
    rt = _boot(
        tmp_path, extra_plugins={"character-admin": character_admin_plugin}
    )
    try:
        rt.ctx.require("character-admin").set("char", "reviewer")
        requests: list[LLMRequest] = []
        rt.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(rt, "char")
        try:
            handle.followup("你好")
        finally:
            handle.dispose()

        assert "代码审查者" in _system_text(requests[0])
    finally:
        rt.close()


def test_system_prompt_override_reaches_the_request(tmp_path: Path) -> None:
    rt = _boot(
        tmp_path,
        extra_plugins={"system-prompt-admin": system_prompt_admin_plugin},
    )
    try:
        rt.ctx.require("system-prompt-admin").override("ov", "附加规则：RAW-7")
        requests: list[LLMRequest] = []
        rt.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(rt, "ov")
        try:
            handle.followup("你好")
        finally:
            handle.dispose()

        assert "RAW-7" in _system_text(requests[0])
    finally:
        rt.close()


def test_prompt_change_mid_session_is_logged_and_used(tmp_path: Path) -> None:
    rt = _boot(
        tmp_path, extra_plugins={"character-admin": character_admin_plugin}
    )
    try:
        requests: list[LLMRequest] = []
        rt.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))

        handle = _agent(rt, "chg")
        try:
            handle.followup("第一句")
            rt.ctx.require("character-admin").set("chg", "teacher")
            handle.followup("第二句")
        finally:
            handle.dispose()

        assert "耐心的老师" not in _system_text(requests[0])
        assert "耐心的老师" in _system_text(requests[-1])

        events = rt.ctx.require(SESSIONS_SERVICE).events("chg")
        system_events = [event for event in events if event.kind == SYSTEM_MESSAGE]
        assert system_events, "提示词变了，却没有追加 system/message"
        assert "耐心的老师" in system_events[-1].payload["content"]
    finally:
        rt.close()


def test_prompt_without_content_adds_no_empty_system_message(tmp_path: Path) -> None:
    """提示词装配结果为空时，不要给模型塞一条空 system 消息。

    这里刻意不用完整产品装配：`tool-shell` 会给 prompt 贡献一段工具说明，
    那样 projection 里本来就应该有一条非空 system 消息。
    """
    from plugins.base.prompt import PromptAssembler
    from plugins.base.sessions import SESSION_START, SessionStore, project_messages

    prompt = PromptAssembler()
    rendered = prompt.render(session_id="s")
    assert rendered.text == ""

    store = SessionStore()
    session = store.create("s", system_prompt=rendered.text)
    session.append("turn/start", {"turn": 1, "text": "你好"})
    messages = project_messages(session.events)
    assert [(m.role, m.content) for m in messages] == [("user", "你好")]
    assert session.events[0].kind == SESSION_START


# ---------------------------------------------------------------------------
# 4. 停用工具 → 模型立即看不到
# ---------------------------------------------------------------------------


def test_disabling_a_tool_hides_it_from_the_next_request(tmp_path: Path) -> None:
    rt = _boot(tmp_path)
    try:
        requests: list[LLMRequest] = []
        rt.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))
        tools = rt.ctx.require(TOOL_ADMIN_SERVICE)

        handle = _agent(rt, "toggle")
        try:
            handle.followup("第一句")
            assert "calc" in {spec.name for spec in requests[-1].tools}

            tools.disable("calc")
            handle.followup("第二句")
            assert "calc" not in {spec.name for spec in requests[-1].tools}

            tools.enable("calc")
            handle.followup("第三句")
            assert "calc" in {spec.name for spec in requests[-1].tools}
        finally:
            handle.dispose()
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 5. 会话隔离
# ---------------------------------------------------------------------------


def test_two_sessions_do_not_share_history(tmp_path: Path) -> None:
    rt = _boot(tmp_path, replies=["收到", "不知道", "7391"])
    try:
        first = _agent(rt, "iso-a")
        try:
            first.followup("记住 7391")
        finally:
            first.dispose()

        # 第二条会话刚建立：只有自己的 system 消息，没有别人的对话。
        second = _agent(rt, "iso-b")
        try:
            early = rt.ctx.require(SESSIONS_SERVICE).messages("iso-b")
            assert [m.role for m in early] == ["system"]
            assert not any("7391" in m.text for m in early)
        finally:
            second.dispose()

        sessions = rt.ctx.require(SESSIONS_SERVICE)
        assert "7391" in " ".join(
            message.text for message in sessions.messages("iso-a")
        )
        assert "7391" not in " ".join(
            message.text for message in sessions.messages("iso-b")
        )
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 6. 热插拔：运行中换模型路由，产品不断档
# ---------------------------------------------------------------------------


def test_hot_swap_provider_keeps_the_product_running(tmp_path: Path) -> None:
    """换一条模型路由（update entry）之后，同一个 runtime 还能继续跑 turn。"""
    rt = _boot(tmp_path, replies=["旧回答"])
    try:
        handle = _agent(rt, "swap")
        try:
            first = handle.followup("第一句")
            assert first.final_text == "旧回答"

            loader = rt.loader
            children = [
                entry
                for entry in loader.snapshot()
                if entry.path.endswith("llm-provider")
            ]
            assert children
            loader.update(
                "app:llm-provider",
                {
                    "route": "fake",
                    "base_url": "https://offline.invalid/chat",
                    "api_key": "offline",
                    "models": [
                        {
                            "id": "fake-1",
                            "context_window": 128000,
                            "max_output_tokens": 4096,
                        }
                    ],
                    "transport": _reply_transport("新回答"),
                },
            )

            second = handle.followup("第二句")
            assert second.final_text == "新回答"
        finally:
            handle.dispose()
    finally:
        rt.close()


def test_loading_a_capability_plugin_takes_effect_immediately(tmp_path: Path) -> None:
    """运行中装一个能力插件，它的工具立刻出现在下一次请求里。"""
    from plugins.base.tools_admin import tool

    class PingPlugin:
        name = "tool-ping"
        inject = (TOOL_ADMIN_SERVICE,)

        def setup(self, ctx: Context) -> None:
            @tool("ping")
            def ping() -> dict[str, Any]:
                """探针工具。"""
                return {"pong": True}

            ctx.require(TOOL_ADMIN_SERVICE).register(
                ping.definition, ping.func, owner=self.name
            )

    rt = _boot(tmp_path, catalog_extras={"tool-ping": lambda **_: PingPlugin()})
    try:
        requests: list[LLMRequest] = []
        rt.ctx.on(AGENT_REQUEST, lambda request, **_: requests.append(request))
        handle = _agent(rt, "hot")
        try:
            handle.followup("第一句")
            assert "ping" not in {spec.name for spec in requests[-1].tools}

            rt.loader.load({"id": "ping", "name": "tool-ping", "role": "capability"})
            handle.followup("第二句")
            assert "ping" in {spec.name for spec in requests[-1].tools}
        finally:
            handle.dispose()
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 7. 产品政策
# ---------------------------------------------------------------------------


def test_product_policy_rejects_manifest_without_application(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    entries = manifest(
        workspace=tmp_path / "ws",
        sessions=tmp_path / "s",
        replies=["x"],
    )
    with pytest.raises(PolicyError):
        boot(entries, catalog=catalog(), product=True)


def test_product_policy_accepts_when_application_is_present(tmp_path: Path) -> None:
    """补一个 application 条目后，同一份装配单能作为"产品"起来。"""
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    entries = manifest(
        workspace=tmp_path / "ws",
        sessions=tmp_path / "s",
        replies=["x"],
    )

    class FakeApp:
        name = "fake-app"

        def setup(self, ctx: Context) -> None:
            ctx.provide("app-entry", "ok")

    runtime = boot(
        [
            *entries,
            {"id": "app-entry", "name": "fake-app", "role": "application"},
        ],
        catalog={**catalog(), "fake-app": lambda **_: FakeApp()},
        product=True,
    )
    try:
        assert runtime.ctx.require("app-entry") == "ok"
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 8. 多步工具链
# ---------------------------------------------------------------------------


def test_multi_step_tool_chain_stays_consistent(tmp_path: Path) -> None:
    """一步调工具、下一步基于结果再调一次：消息历史必须自洽。"""
    rt = _boot(tmp_path, replies=["先算"])
    try:
        calls: list[str] = []
        rt.ctx.on(AGENT_TOOL_CALL, lambda call, **_: calls.append(call.name))

        # 剧本：第一轮算数、第二轮写文件、第三轮收尾
        transport = _script_transport(
            [
                [ToolCall("c1", "calc", '{"expression":"6*7"}')],
                [ToolCall("c2", "write_file", '{"path":"answer.txt","content":"42"}')],
                "完成",
            ]
        )
        _swap_transport(rt, transport)

        handle = _agent(rt, "chain")
        try:
            result = handle.followup("算 6*7 然后写进 answer.txt")
        finally:
            handle.dispose()

        assert calls == ["calc", "write_file"]
        assert result.final_text == "完成"
        assert (tmp_path / "ws" / "answer.txt").read_text(encoding="utf-8") == "42"

        messages = rt.ctx.require(SESSIONS_SERVICE).messages("chain")
        for index, message in enumerate(messages):
            if message.role == "assistant" and message.tool_calls:
                ids = {call.id for call in message.tool_calls}
                results = {
                    later.tool_call_id
                    for later in messages[index + 1 :]
                    if later.role == "tool"
                }
                assert ids <= results, f"悬空 tool_calls：{ids - results}"
        assistants = [m for m in messages if m.role == ASSISTANT]
        assert len(assistants) == 3
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 剧本传输辅助
# ---------------------------------------------------------------------------


def _reply_transport(text: str):
    import json

    def transport(url: str, headers: Any, payload: Any, timeout: float):
        events = [
            json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False),
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": len(text) // 4},
                },
                ensure_ascii=False,
            ),
        ]
        return [f"data: {event}" for event in events] + ["data: [DONE]"]

    return transport


def _script_transport(script: list[Any]):
    """每一轮返回一段剧本：字符串 = 文本回答，ToolCall 列表 = 发起工具调用。"""
    import json

    state = {"index": 0}

    def transport(url: str, headers: Any, payload: Any, timeout: float):
        index = min(state["index"], len(script) - 1)
        state["index"] += 1
        step = script[index]
        if isinstance(step, str):
            events = [
                json.dumps(
                    {"choices": [{"delta": {"content": step}}]}, ensure_ascii=False
                ),
                json.dumps(
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
                json.dumps(
                    {"choices": [{"delta": {"tool_calls": deltas}}]},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    }
                ),
            ]
        return [f"data: {event}" for event in events] + ["data: [DONE]"]

    return transport


def _swap_transport(rt: Any, transport: Any) -> None:
    """把一个剧本 transport 换到当前 llm-provider 的适配器上。"""
    llm = rt.ctx.require(LLM_SERVICE)
    adapter = llm._providers["fake"]
    adapter._transport = transport
