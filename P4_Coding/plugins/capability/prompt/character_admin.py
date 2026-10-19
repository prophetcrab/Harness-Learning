"""character-admin —— 预设角色模板（能力插件）。

一个"角色"就是一段写进系统提示词的文本（人设、语气、工作方式）。本插件：

    · 内置一组预设（default / coder / reviewer / teacher）；
    · 装配单可以追加或覆盖预设（`characters`），也可以指定全局默认（`default`）；
    · **按会话**选择角色：`set(session_id, name)`（不同会话可以不同角色，
      与"不同会话选不同模型"是同一层语义）；
    · 往提示词里注册一个 section（order=10，靠前），内容由当前会话的角色决定。

角色选择只影响**提示词**，不改模型、不改工具面——那两样各有各的插件。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core import Context


class PromptError(RuntimeError):
    """提示词装配的错误基类。"""


class DuplicateSectionError(PromptError):
    """同名 section 重复注册（fail loud：要替换先注销）。"""


class MissingVariableError(PromptError):
    """section 里用了一个没有来源的变量。"""


class UnknownSectionError(PromptError):
    """操作一个不存在的 section（fail loud：防止拼写错误静默生效）。"""


# === 本地副本（来自 plugins/base/prompt.py；插件之间不 import，见决策 0053）===
class PromptAssembler:
    """section 注册表 + 变量注册表 + 渲染。

    用法（插件作者视角）：

        sections = ctx.require("prompt")
        sections.section("identity", "你是一名严谨的工程师。", order=10)
        sections.variable("cwd", lambda context: context.get("cwd", ""))
        rendered = sections.render(session_id="s1", cwd="/repo")

    注册方法都返回注销句柄（幂等）：在插件 setup 里注册、把句柄作为 setup 的
    返回值交出去，卸载时就会自动消失（注册即回卷）。
    """

    def __init__(self) -> None:
        self._sections: dict[str, _Section] = {}
        self._variables: dict[str, VariableResolver] = {}
        self._seq = 0

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------

    def section(self, name: str, text: SectionText, *, order: int = 100) -> Callable[[], None]:
        """注册一个 section；同名已存在 → DuplicateSectionError。"""
        if not isinstance(name, str) or not name:
            raise ValueError(f"section 名必须是非空字符串，收到：{name!r}")
        if not isinstance(text, str) and not callable(text):
            raise TypeError(f"section {name!r} 的 text 必须是字符串或可调用对象")
        if name in self._sections:
            raise DuplicateSectionError(
                f"section 已存在：{name!r}（要替换先注销旧的）"
            )
        self._seq += 1
        record = _Section(name=name, text=text, order=int(order), seq=self._seq)
        self._sections[name] = record

        def unregister() -> None:
            if self._sections.get(name) is record:
                self._sections.pop(name, None)

        return unregister

    def variable(self, name: str, resolver: VariableResolver) -> Callable[[], None]:
        """注册一个变量解析器；同名重复 → DuplicateSectionError（同一套纪律）。"""
        if not isinstance(name, str) or not name:
            raise ValueError(f"变量名必须是非空字符串，收到：{name!r}")
        if not callable(resolver):
            raise TypeError(f"变量 {name!r} 的 resolver 必须可调用")
        if name in self._variables:
            raise DuplicateSectionError(
                f"变量已存在：{name!r}（要替换先注销旧的）"
            )
        self._variables[name] = resolver

        def unregister() -> None:
            if self._variables.get(name) is resolver:
                self._variables.pop(name, None)

        return unregister

    # ------------------------------------------------------------------
    # 开关（system-prompt-admin 的"编辑"落点）
    # ------------------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> None:
        """打开 / 关掉一个 section；不存在 → UnknownSectionError。"""
        section = self._sections.get(name)
        if section is None:
            known = "、".join(self.section_names) or "（还没有 section）"
            raise UnknownSectionError(f"section 不存在：{name!r}（当前：{known}）")
        section.enabled = bool(enabled)

    def is_enabled(self, name: str) -> bool:
        section = self._sections.get(name)
        if section is None:
            raise UnknownSectionError(f"section 不存在：{name!r}")
        return section.enabled

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    @property
    def section_names(self) -> tuple[str, ...]:
        """已注册的 section 名（按渲染顺序）。"""
        return tuple(
            item.name for item in sorted(self._sections.values(), key=lambda s: (s.order, s.seq))

        )
    @property
    def variable_names(self) -> tuple[str, ...]:
        return tuple(self._variables)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, **context: Any) -> RenderedPrompt:
        """按当前注册表渲染；**不缓存**（每次现算，调用方可按 step 调）。

        关闭的 section 完全不参与：它的文本函数不会被调用、变量也不会被解析。
        """
        traces: list[SectionTrace] = []
        used: dict[str, Any] = {}
        for section in sorted(self._sections.values(), key=lambda s: (s.order, s.seq)):
            if not section.enabled:
                traces.append(
                    SectionTrace(name=section.name, order=section.order, text="", enabled=False)
                )
                continue
            raw = section.text(context) if callable(section.text) else section.text
            if raw is None:
                raw = ""
            if not isinstance(raw, str):
                raise PromptError(
                    f"section {section.name!r} 渲染出了非字符串：{type(raw).__name__}"
                )
            resolved = _VARIABLE_PATTERN.sub(
                lambda match, where=section.name: _substitute(
                    match, where, context, self._variables, used
                ),
                raw,
            )
            traces.append(
                SectionTrace(
                    name=section.name,
                    order=section.order,
                    text=resolved,
                    enabled=True,
                )
            )
        body = "\n\n".join(
            trace.text.strip() for trace in traces if trace.enabled and trace.text.strip()
        )
        return RenderedPrompt(text=body, sections=tuple(traces), variables=used)

@dataclass(frozen=True)
class RenderedPrompt:
    """渲染结果：最终文本 + 装配单 + 用到的变量。"""

    text: str
    sections: tuple[SectionTrace, ...]
    variables: Mapping[str, Any]

    @property
    def fingerprint(self) -> str:
        """文本指纹——"这一版提示词变没变"（日志只在变化时追加）。"""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

@dataclass(frozen=True)
class SectionTrace:
    """一个 section 在这次渲染里的样子（装配单的一项）。"""

    name: str
    order: int
    text: str
    enabled: bool

SectionText = str | Callable[[Mapping[str, Any]], "str | None"]

VariableResolver = Callable[[Mapping[str, Any]], Any]

@dataclass
class _Section:
    name: str
    text: SectionText
    order: int
    seq: int
    enabled: bool = True

_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

def _substitute(
    match: re.Match[str],
    where: str,
    context: Mapping[str, Any],
    variables: Mapping[str, VariableResolver],
    used: dict[str, Any],
) -> str:
    """替换一个 `{{name}}`：context 优先，其次变量注册表；都没有就 fail loud。"""
    name = match.group(1)
    if name in context:
        value = context[name]
    elif name in variables:
        value = variables[name](context)
    else:
        known = "、".join(sorted(set(context) | set(variables))) or "（没有任何变量）"
        raise MissingVariableError(
            f"section {where!r} 引用了未定义的变量 {{{{{name}}}}}（可用：{known}）"
        )
    used[name] = value
    return "" if value is None else str(value)

# --- 服务/事件名（字符串，本地复刻）---
PROMPT_SERVICE = "prompt"



#: 角色服务的名字。
CHARACTER_SERVICE = "character-admin"

#: 角色 section 的位置：靠前（人设先于具体说明）。
CHARACTER_SECTION_ORDER = 10

#: 内置预设（简洁、可直接用；用户可以在装配单里整段覆盖）。
BUILTIN_CHARACTERS: dict[str, str] = {
    "default": "你是一个乐于助人的助手。回答准确、简洁；不确定时如实说明，不要编造。",
    "coder": (
        "你是一名严谨的软件工程师。优先读懂现有代码与约定，再动手修改；"
        "改动尽量小而完整，并说明你验证过什么。"
    ),
    "reviewer": (
        "你是一名严格的代码审查者。先指出正确性与安全问题，再谈风格；"
        "每条意见给出理由与可执行的修改建议。"
    ),
    "teacher": (
        "你是一名耐心的老师。先讲清概念与为什么，再给例子；"
        "用对方能听懂的语言，避免堆砌术语。"
    ),
}

class CharacterAdminService:
    """角色模板表 + 按会话的选择。"""

    def __init__(
        self,
        characters: Mapping[str, str] | None = None,
        *,
        default: str | None = None,
    ) -> None:
        self._characters: dict[str, str] = dict(BUILTIN_CHARACTERS)
        if characters:
            for name, text in characters.items():
                if not isinstance(name, str) or not name:
                    raise ValueError(f"角色名必须是非空字符串：{name!r}")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"角色 {name!r} 的文本必须是非空字符串")
                self._characters[name] = text
        if default is not None and default not in self._characters:
            raise ValueError(
                f"默认角色不存在：{default!r}（可用：{'、'.join(self.list())}）"
            )
        self._default = default
        #: session_id → 角色名
        self._sessions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def list(self) -> tuple[str, ...]:
        """全部角色名（按定义顺序：内置在前，装配单追加在后）。"""
        return tuple(self._characters)

    def describe(self, name: str) -> str:
        """角色文本；未知角色 → ValueError（fail loud）。"""
        try:
            return self._characters[name]
        except KeyError:
            raise ValueError(
                f"角色不存在：{name!r}（可用：{'、'.join(self.list())}）"
            ) from None

    def active_name(self, session_id: str | None) -> str | None:
        """某个会话当前的角色名（没选就用默认；都没有 → None）。"""
        if session_id is not None and session_id in self._sessions:
            return self._sessions[session_id]
        return self._default

    def get(self, session_id: str | None) -> str | None:
        """某个会话当前的**角色文本**（None = 不贡献提示词）。"""
        name = self.active_name(session_id)
        return self._characters[name] if name is not None else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "characters": list(self.list()),
            "default": self._default,
            "sessions": dict(self._sessions),
        }

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def add(self, name: str, text: str) -> None:
        """追加一个角色；重名 → ValueError（要替换请换名字或重新装配）。"""
        if not isinstance(name, str) or not name:
            raise ValueError(f"角色名必须是非空字符串：{name!r}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"角色 {name!r} 的文本必须是非空字符串")
        if name in self._characters:
            raise ValueError(f"角色已存在：{name!r}（不静默覆盖）")
        self._characters[name] = text

    def set(self, session_id: str, name: str) -> None:
        """给某个会话选角色；未知角色 → ValueError。"""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session_id 必须是非空字符串：{session_id!r}")
        self.describe(name)  # 校验（未知角色在这里 fail loud）
        self._sessions[session_id] = name

    def clear(self, session_id: str) -> None:
        """清掉某个会话的选择（回到默认）。"""
        self._sessions.pop(session_id, None)

    # ------------------------------------------------------------------
    # 提示词 section
    # ------------------------------------------------------------------

    def text_for(self, context: Mapping[str, Any]) -> str:
        """提示词 section 的正文（按渲染上下文里的 session_id 取角色）。"""
        return self.get(context.get("session_id")) or ""

class CharacterAdminPlugin:
    """提供 `character-admin` 服务，并注册角色提示词 section。"""

    name = "character-admin"
    inject = (PROMPT_SERVICE,)

    def __init__(
        self,
        *,
        characters: Mapping[str, str] | None = None,
        default: str | None = None,
    ) -> None:
        self._characters = dict(characters or {})
        self._default = default

    def setup(self, ctx: Context):
        prompt: PromptAssembler = ctx.require(PROMPT_SERVICE)
        service = CharacterAdminService(self._characters, default=self._default)
        ctx.provide(CHARACTER_SERVICE, service)
        # 返回注销句柄：插件卸载时角色 section 随之消失（注册即回卷）。
        return prompt.section("character", service.text_for, order=CHARACTER_SECTION_ORDER)

def character_admin_plugin(**config: Any) -> CharacterAdminPlugin:
    """工厂：装配单的 `config` 直接喂给构造参数。"""
    return CharacterAdminPlugin(**config)

__all__ = [
    "BUILTIN_CHARACTERS",
    "CHARACTER_SECTION_ORDER",
    "CHARACTER_SERVICE",
    "CharacterAdminPlugin",
    "CharacterAdminService",
    "character_admin_plugin",
]
