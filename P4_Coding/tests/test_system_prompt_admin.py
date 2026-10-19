"""system-prompt-admin 验收：检查（inspect）与编辑（override / disable）。"""

from __future__ import annotations

import pytest

from core import Context
from plugins.base.prompt import PROMPT_SERVICE, UnknownSectionError, prompt_plugin
from plugins.capability.prompt.system_prompt_admin import (
    OVERRIDE_SECTION,
    SYSTEM_PROMPT_ADMIN_SERVICE,
    system_prompt_admin_plugin,
)


def booted(**prompt_config) -> tuple[Context, object, object]:
    ctx = Context()
    ctx.load_plugin(
        prompt_plugin(
            **{
                "sections": [
                    {"name": "identity", "order": 10, "text": "你是一名工程师。"},
                    {"name": "rules", "order": 20, "text": "先读代码。"},
                ],
                **prompt_config,
            }
        )
    )
    ctx.load_plugin(system_prompt_admin_plugin())
    return ctx, ctx.require(PROMPT_SERVICE), ctx.require(SYSTEM_PROMPT_ADMIN_SERVICE)


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------


def test_inspect_matches_actual_render():
    _ctx, prompt, admin = booted()
    actual = prompt.render(session_id="s1")
    view = admin.inspect(session_id="s1")

    assert view["text"] == actual.text
    assert view["fingerprint"] == actual.fingerprint


def test_inspect_reports_section_table():
    _ctx, _prompt, admin = booted()
    view = admin.inspect()
    assert [(s["name"], s["order"], s["enabled"]) for s in view["sections"]] == [
        ("identity", 10, True),
        ("rules", 20, True),
        (OVERRIDE_SECTION, 900, True),
    ]
    assert all(isinstance(s["chars"], int) for s in view["sections"])


def test_inspect_reports_variables():
    ctx, prompt, _admin = booted()
    prompt.variable("cwd", lambda c: "/repo")
    prompt.section("where", "目录：{{cwd}}", order=30)
    view = ctx.require(SYSTEM_PROMPT_ADMIN_SERVICE).inspect()
    assert view["variables"] == {"cwd": "/repo"}


# ---------------------------------------------------------------------------
# 编辑：按会话追加
# ---------------------------------------------------------------------------


def test_override_appends_to_one_session_only():
    _ctx, prompt, admin = booted()
    admin.override("s1", "请用中文回答。")

    text_a = prompt.render(session_id="s1").text
    text_b = prompt.render(session_id="s2").text
    assert "请用中文回答。" in text_a
    assert "请用中文回答。" not in text_b
    assert text_a.index("先读代码。") < text_a.index("请用中文回答。")  # 靠后


def test_override_is_visible_in_inspect():
    _ctx, _prompt, admin = booted()
    admin.override("s1", "补充说明")
    view = admin.inspect(session_id="s1")
    assert view["overrides"] == {"s1": "补充说明"}
    assert "补充说明" in view["text"]


def test_clear_override():
    _ctx, prompt, admin = booted()
    admin.override("s1", "补充说明")
    admin.clear_override("s1")
    assert "补充说明" not in prompt.render(session_id="s1").text
    admin.override("s1", "又被清空")
    admin.override("s1", "")          # 空字符串也等于清除
    assert admin.inspect(session_id="s1")["overrides"] == {}


def test_override_without_session_id_contributes_nothing():
    _ctx, prompt, admin = booted()
    admin.override("s1", "补充说明")
    assert "补充说明" not in prompt.render().text


# ---------------------------------------------------------------------------
# 编辑：开关 section
# ---------------------------------------------------------------------------


def test_disable_and_enable_section():
    _ctx, prompt, admin = booted()
    admin.disable("rules")
    assert "先读代码。" not in prompt.render().text
    assert admin.disabled_sections() == ("rules",)
    assert admin.inspect()["sections"][1]["enabled"] is False

    admin.enable("rules")
    assert "先读代码。" in prompt.render().text
    assert admin.disabled_sections() == ()


def test_disable_unknown_section_fails_loud():
    _ctx, _prompt, admin = booted()
    with pytest.raises(UnknownSectionError):
        admin.disable("nope")


def test_disable_twice_is_idempotent():
    _ctx, prompt, admin = booted()
    admin.disable("rules")
    admin.disable("rules")
    assert admin.disabled_sections() == ("rules",)
    assert "先读代码。" not in prompt.render().text


# ---------------------------------------------------------------------------
# 生命周期：卸载时恢复
# ---------------------------------------------------------------------------


def test_unload_restores_disabled_sections():
    ctx, prompt, admin = booted()
    admin.disable("rules")
    assert "先读代码。" not in prompt.render().text

    ctx.unload_plugin("system-prompt-admin")
    assert "先读代码。" in prompt.render().text  # 恢复
    assert prompt.is_enabled("rules")


def test_unload_removes_override_section_and_content():
    ctx, prompt, admin = booted()
    admin.override("s1", "补充说明")
    assert "补充说明" in prompt.render(session_id="s1").text

    ctx.unload_plugin("system-prompt-admin")
    assert "补充说明" not in prompt.render(session_id="s1").text
    assert OVERRIDE_SECTION not in prompt.section_names


def test_admin_waits_for_prompt():
    ctx = Context()
    ctx.load_plugin(system_prompt_admin_plugin())
    assert ctx.pending_plugins == ["system-prompt-admin"]
    ctx.load_plugin(prompt_plugin())
    assert ctx.active_plugins == ["system-prompt-admin", "prompt"]
