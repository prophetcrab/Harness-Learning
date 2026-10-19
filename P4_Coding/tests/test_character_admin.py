"""character-admin 验收：预设角色模板 + 按会话选择。"""

from __future__ import annotations

import pytest

from core import Context
from plugins.base.prompt import PROMPT_SERVICE, prompt_plugin
from plugins.capability.prompt.character_admin import (
    BUILTIN_CHARACTERS,
    CHARACTER_SERVICE,
    character_admin_plugin,
)


def booted(**config) -> tuple[Context, object, object]:
    ctx = Context()
    ctx.load_plugin(prompt_plugin())
    ctx.load_plugin(character_admin_plugin(**config))
    return ctx, ctx.require(PROMPT_SERVICE), ctx.require(CHARACTER_SERVICE)


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------


def test_builtin_characters_are_listed():
    _ctx, _prompt, characters = booted()
    assert set(characters.list()) == set(BUILTIN_CHARACTERS)


def test_describe_and_unknown_name():
    _ctx, _prompt, characters = booted()
    assert "软件工程师" in characters.describe("coder")
    with pytest.raises(ValueError, match="角色不存在"):
        characters.describe("nope")


def test_nothing_selected_contributes_nothing():
    _ctx, prompt, _characters = booted()
    assert prompt.render(session_id="s1").text == ""


# ---------------------------------------------------------------------------
# 按会话选择
# ---------------------------------------------------------------------------


def test_two_sessions_can_use_different_characters():
    _ctx, prompt, characters = booted()
    characters.set("s1", "coder")
    characters.set("s2", "teacher")

    text_a = prompt.render(session_id="s1").text
    text_b = prompt.render(session_id="s2").text
    assert "软件工程师" in text_a
    assert "耐心的老师" in text_b
    assert text_a != text_b


def test_session_can_change_character_mid_conversation():
    _ctx, prompt, characters = booted()
    characters.set("s1", "coder")
    assert "软件工程师" in prompt.render(session_id="s1").text

    characters.set("s1", "reviewer")
    assert "代码审查者" in prompt.render(session_id="s1").text


def test_unknown_character_fails_loud_and_keeps_previous():
    _ctx, _prompt, characters = booted()
    characters.set("s1", "coder")
    with pytest.raises(ValueError, match="角色不存在"):
        characters.set("s1", "nope")
    assert characters.active_name("s1") == "coder"


def test_clear_returns_to_default():
    _ctx, prompt, characters = booted(default="coder")
    characters.set("s1", "teacher")
    assert "耐心的老师" in prompt.render(session_id="s1").text

    characters.clear("s1")
    assert characters.active_name("s1") == "coder"
    assert "软件工程师" in prompt.render(session_id="s1").text


def test_default_applies_to_sessions_without_choice():
    _ctx, prompt, characters = booted(default="teacher")
    assert "耐心的老师" in prompt.render(session_id="any").text
    assert "耐心的老师" in prompt.render().text  # 没有 session_id 也用默认


def test_snapshot_is_readable():
    _ctx, _prompt, characters = booted(default="default")
    characters.set("s1", "coder")
    snap = characters.snapshot()
    assert snap["default"] == "default"
    assert snap["sessions"] == {"s1": "coder"}
    assert "coder" in snap["characters"]


# ---------------------------------------------------------------------------
# 装配单配置：追加 / 覆盖
# ---------------------------------------------------------------------------


def test_config_can_add_and_override_characters():
    _ctx, prompt, characters = booted(
        characters={"pirate": "你是一名海盗。", "coder": "你是一名极简主义者。"},
        default="pirate",
    )
    assert characters.describe("pirate") == "你是一名海盗。"
    assert characters.describe("coder") == "你是一名极简主义者。"  # 覆盖内置
    assert "海盗" in prompt.render().text  # 默认角色生效


def test_config_with_unknown_default_fails_loud():
    with pytest.raises(ValueError, match="默认角色不存在"):
        booted(default="nope")


def test_add_custom_character():
    _ctx, prompt, characters = booted()
    characters.add("poet", "你是一名诗人。")
    characters.set("s1", "poet")
    assert "诗人" in prompt.render(session_id="s1").text
    with pytest.raises(ValueError, match="已存在"):
        characters.add("poet", "重复")


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_unloading_character_admin_removes_its_prompt_section():
    ctx, prompt, characters = booted()
    characters.set("s1", "coder")
    assert prompt.render(session_id="s1").text

    ctx.unload_plugin("character-admin")
    assert prompt.render(session_id="s1").text == ""  # section 随插件消失
    assert CHARACTER_SERVICE  # 服务名常量只是引用，避免 lint


def test_character_admin_waits_for_prompt():
    """必要依赖是 prompt：先装它会是 pending。"""
    from core import Context

    ctx = Context()
    ctx.load_plugin(character_admin_plugin())
    assert ctx.pending_plugins == ["character-admin"]
    ctx.load_plugin(prompt_plugin())
    assert ctx.active_plugins == ["character-admin", "prompt"]
