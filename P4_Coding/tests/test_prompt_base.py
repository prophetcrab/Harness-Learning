"""prompt 基础插件验收：有序 section / 运行时变量 / 渲染不缓存。

对应结构要求（用户定义）：

    prompt（base，必要依赖：无）
    character-admin（预设角色模板）
    system-prompt-admin（检查与编辑）   ← 后者另有两个测试文件
"""

from __future__ import annotations

import pytest

from core import Context
from plugins.base.prompt import (
    PROMPT_SERVICE,
    DuplicateSectionError,
    MissingVariableError,
    PromptAssembler,
    UnknownSectionError,
    prompt_plugin,
)


def booted(**config) -> tuple[Context, PromptAssembler]:
    ctx = Context()
    ctx.load_plugin(prompt_plugin(**config))
    return ctx, ctx.require(PROMPT_SERVICE)


# ---------------------------------------------------------------------------
# 有序装配
# ---------------------------------------------------------------------------


def test_sections_render_in_order():
    _ctx, prompt = booted()
    prompt.section("late", "第三", order=30)
    prompt.section("early", "第一", order=10)
    prompt.section("middle", "第二", order=20)
    assert prompt.render().text == "第一\n\n第二\n\n第三"


def test_same_order_uses_registration_order():
    _ctx, prompt = booted()
    prompt.section("a", "A", order=10)
    prompt.section("b", "B", order=10)
    assert prompt.render().text == "A\n\nB"


def test_duplicate_section_fails_loud():
    _ctx, prompt = booted()
    prompt.section("x", "原始")
    with pytest.raises(DuplicateSectionError, match="已存在"):
        prompt.section("x", "覆盖")
    assert prompt.render().text == "原始"  # 原 section 没被动


def test_unregister_removes_section_and_is_idempotent():
    _ctx, prompt = booted()
    off = prompt.section("x", "内容")
    off()
    off()
    assert prompt.render().text == ""
    assert prompt.section_names == ()


def test_empty_render_is_empty_string():
    _ctx, prompt = booted()
    assert prompt.render().text == ""
    assert prompt.render().fingerprint


# ---------------------------------------------------------------------------
# 变量
# ---------------------------------------------------------------------------


def test_context_values_take_precedence():
    _ctx, prompt = booted()
    prompt.variable("name", lambda ctx: "来自变量")
    prompt.section("s", "你好，{{name}}！")
    assert prompt.render(name="来自上下文").text == "你好，来自上下文！"
    assert prompt.render().text == "你好，来自变量！"


def test_missing_variable_fails_loud_with_section_name():
    _ctx, prompt = booted()
    prompt.section("greeting", "你好，{{who}}！")
    with pytest.raises(MissingVariableError, match="greeting"):
        prompt.render()


def test_variable_none_renders_as_empty():
    _ctx, prompt = booted()
    prompt.variable("maybe", lambda ctx: None)
    prompt.section("s", "前{{maybe}}后")
    assert prompt.render().text == "前后"


def test_variables_are_resolved_per_render():
    _ctx, prompt = booted()
    values = iter(["第一次", "第二次"])
    prompt.variable("v", lambda ctx: next(values))
    prompt.section("s", "{{v}}")
    assert prompt.render().text == "第一次"
    assert prompt.render().text == "第二次"  # 不缓存


def test_duplicate_variable_fails_loud():
    _ctx, prompt = booted()
    prompt.variable("v", lambda ctx: 1)
    with pytest.raises(DuplicateSectionError):
        prompt.variable("v", lambda ctx: 2)


# ---------------------------------------------------------------------------
# 可调用 section / 开关
# ---------------------------------------------------------------------------


def test_callable_section_sees_context_and_can_skip():
    _ctx, prompt = booted()
    prompt.section("cwd", lambda ctx: f"当前目录：{ctx['cwd']}" if ctx.get("cwd") else None)
    assert prompt.render(cwd="/repo").text == "当前目录：/repo"
    assert prompt.render().text == ""


def test_disabled_section_is_not_rendered_nor_called():
    _ctx, prompt = booted()
    calls: list[str] = []

    def text(ctx):
        calls.append("called")
        return "内容"

    prompt.section("s", text)
    prompt.set_enabled("s", False)
    rendered = prompt.render()
    assert rendered.text == ""
    assert calls == []
    assert rendered.sections[0].enabled is False

    prompt.set_enabled("s", True)
    assert prompt.render().text == "内容"


def test_unknown_section_switch_fails_loud():
    _ctx, prompt = booted()
    with pytest.raises(UnknownSectionError):
        prompt.set_enabled("nope", False)
    with pytest.raises(UnknownSectionError):
        prompt.is_enabled("nope")


# ---------------------------------------------------------------------------
# 装配单（可追溯）
# ---------------------------------------------------------------------------


def test_trace_and_fingerprint():
    _ctx, prompt = booted()
    prompt.section("a", "A", order=1)
    prompt.section("b", "{{x}}", order=2)
    rendered = prompt.render(x="B")
    assert rendered.text == "A\n\nB"
    assert [(s.name, s.order, s.text, s.enabled) for s in rendered.sections] == [
        ("a", 1, "A", True),
        ("b", 2, "B", True),
    ]
    assert rendered.variables == {"x": "B"}

    before = rendered.fingerprint
    prompt.section("c", "C", order=3)
    assert prompt.render(x="B").fingerprint != before
    assert prompt.render(x="B").fingerprint == prompt.render(x="B").fingerprint


# ---------------------------------------------------------------------------
# 插件生命周期 / 配置
# ---------------------------------------------------------------------------


def test_config_sections_are_loaded():
    _ctx, prompt = booted(
        sections=[
            {"name": "identity", "order": 10, "text": "你是一名工程师。"},
            {"name": "rules", "order": 20, "text": "先读代码再动手。"},
        ]
    )
    assert prompt.render().text == "你是一名工程师。\n\n先读代码再动手。"


def test_bad_config_fails_loud():
    with pytest.raises(ValueError):
        booted(sections=[{"order": 1, "text": "缺 name"}])
    with pytest.raises(ValueError):
        booted(sections=[{"name": "x", "text": 123}])


def test_unloading_prompt_removes_service():
    ctx, _prompt = booted()
    ctx.unload_plugin("prompt")
    assert ctx.get(PROMPT_SERVICE) is None


def test_section_registered_in_setup_is_rolled_back_with_plugin():
    class Contributor:
        name = "contributor"
        inject = (PROMPT_SERVICE,)

        def setup(self, ctx):
            prompt = ctx.require(PROMPT_SERVICE)
            return prompt.section("from-plugin", "插件贡献的内容")

    ctx, prompt = booted()
    ctx.load_plugin(Contributor())
    assert prompt.render().text == "插件贡献的内容"
    ctx.unload_plugin("contributor")
    assert prompt.render().text == ""
