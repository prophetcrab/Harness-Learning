"""system-prompt-admin —— 系统提示词的检查与编辑（能力插件）。

它是提示词的"管理面"，做三件事：

    inspect(**context)         看当前渲染结果 + 装配单（每段 section 的文本/顺序/
                               开关状态、用到的变量、文本指纹）
    override(session_id, text) 给某个会话追加一段"用户补充说明"（order=900，靠后）
    disable / enable(name)     关掉 / 打开某个 section（编辑别人的贡献）

**编辑是运行时的、不回写装配单**（0042 的立场）：重启回到装配单声明的内容。

**卸载会恢复**：本插件关掉的 section 在卸载时重新打开、追加的 override section
随 effect 回卷——不会留下"某个 section 被永久关掉"的垃圾状态。
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



#: 管理服务的名字。
SYSTEM_PROMPT_ADMIN_SERVICE = "system-prompt-admin"

#: override section 的位置：排在所有常规 section 之后。
OVERRIDE_SECTION = "admin:override"
OVERRIDE_SECTION_ORDER = 900

class SystemPromptAdminService:
    """检查与编辑：只读写 prompt 服务本身，不缓存提示词文本。"""

    def __init__(self, prompt: PromptAssembler) -> None:
        self._prompt = prompt
        #: session_id → 追加说明
        self._overrides: dict[str, str] = {}
        #: 本插件关掉的 section（卸载时恢复）
        self._disabled: list[str] = []

    # ------------------------------------------------------------------
    # 检查
    # ------------------------------------------------------------------

    def inspect(self, **context: Any) -> dict[str, Any]:
        """当前提示词的完整视图（就是一次真实渲染的结果）。"""
        rendered = self._prompt.render(**context)
        return {
            "text": rendered.text,
            "fingerprint": rendered.fingerprint,
            "sections": [
                {
                    "name": trace.name,
                    "order": trace.order,
                    "enabled": trace.enabled,
                    "chars": len(trace.text),
                }
                for trace in rendered.sections
            ],
            "variables": dict(rendered.variables),
            "overrides": dict(self._overrides),
        }

    def disabled_sections(self) -> tuple[str, ...]:
        return tuple(self._disabled)

    # ------------------------------------------------------------------
    # 编辑
    # ------------------------------------------------------------------

    def override(self, session_id: str, text: str) -> None:
        """给某个会话追加一段说明（空字符串 = 清除）。"""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session_id 必须是非空字符串：{session_id!r}")
        if not isinstance(text, str):
            raise TypeError(f"override 文本必须是字符串，收到：{type(text)!r}")
        if text.strip():
            self._overrides[session_id] = text
        else:
            self._overrides.pop(session_id, None)

    def clear_override(self, session_id: str) -> None:
        self._overrides.pop(session_id, None)

    def disable(self, name: str) -> None:
        """关掉一个 section；不存在 → UnknownSectionError（拼错立刻知道）。"""
        if not self._prompt.is_enabled(name):  # 也存在性检查
            if name not in self._disabled:
                self._disabled.append(name)
            return
        self._prompt.set_enabled(name, False)
        self._disabled.append(name)

    def enable(self, name: str) -> None:
        """打开一个 section（本插件关过的会从"待恢复"名单里移除）。"""
        self._prompt.set_enabled(name, True)
        if name in self._disabled:
            self._disabled.remove(name)

    # ------------------------------------------------------------------
    # 提示词 section
    # ------------------------------------------------------------------

    def text_for(self, context: dict[str, Any]) -> str:
        """override section 的正文：只对设置了 override 的会话有内容。"""
        session_id = context.get("session_id")
        if not isinstance(session_id, str):
            return ""
        return self._overrides.get(session_id, "")

    def cleanup(self) -> None:
        """卸载：恢复本插件关掉的 section（不留下被永久关掉的贡献）。"""
        for name in list(self._disabled):
            try:
                self._prompt.set_enabled(name, True)
            except Exception:  # noqa: BLE001 —— 那个 section 可能已随它的插件消失
                pass
        self._disabled.clear()

class SystemPromptAdminPlugin:
    """提供 `system-prompt-admin` 服务，并注册 override section。"""

    name = "system-prompt-admin"
    inject = (PROMPT_SERVICE,)

    def setup(self, ctx: Context):
        prompt: PromptAssembler = ctx.require(PROMPT_SERVICE)
        service = SystemPromptAdminService(prompt)
        ctx.provide(SYSTEM_PROMPT_ADMIN_SERVICE, service)
        remove_section = prompt.section(
            OVERRIDE_SECTION, service.text_for, order=OVERRIDE_SECTION_ORDER

        )
        def cleanup() -> None:
            remove_section()
            service.cleanup()

        return cleanup

def system_prompt_admin_plugin(**config: Any) -> SystemPromptAdminPlugin:
    """工厂（当前无配置；保留 kwargs 以便装配单写法统一）。"""
    return SystemPromptAdminPlugin(**config)

__all__ = [
    "OVERRIDE_SECTION",
    "OVERRIDE_SECTION_ORDER",
    "SYSTEM_PROMPT_ADMIN_SERVICE",
    "SystemPromptAdminPlugin",
    "SystemPromptAdminService",
    "system_prompt_admin_plugin",
]
