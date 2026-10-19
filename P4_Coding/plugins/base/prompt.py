"""prompt —— 系统提示词装配（基础插件，**必要依赖：无**）。

它只提供"注册 + 渲染"两件事，不认识任何具体内容：

    ctx.require("prompt").section(name, text, order=100)   # 注册一段
    ctx.require("prompt").variable(name, resolver)         # 注册一个变量
    prompt.render(session_id=..., cwd=...)                 # 每 step 调一次

**为什么无依赖**：提示词是 harness 最底层的输入之一，它不该等任何服务就绪；
section 与变量的内容由别的插件按需贡献（character-admin、工具说明书、时间上下文……）。

**不缓存**：每次 render 现算。调用方（agent-loop）按 step 调，并且用
`RenderedPrompt.fingerprint` 判断"变没变、要不要往日志追加"——这是 P2 决策
0007 的语义，避免每 step 重复写一遍系统提示词。

装配单里的配置（可选）：静态 section 列表，方便"纯数据"地加一段提示词。

    {"id": "prompt", "name": "prompt",
     "config": {"sections": [
        {"name": "identity", "order": 10, "text": "你是一名严谨的软件工程师。"}
     ]}}
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core import Context

#: 提示词服务的名字。
PROMPT_SERVICE = "prompt"


class PromptPlugin:
    """提供 `prompt` 服务（有序 section + 变量 + 渲染）。

    装配单里应声明 `role="base"`。构造参数只有可选的静态 section 列表。
    """

    name = "prompt"

    def __init__(self, *, sections: Sequence[Mapping[str, Any]] = ()) -> None:
        self._sections = tuple(dict(section) for section in sections)

    def setup(self, ctx: Context) -> None:
        assembler = PromptAssembler()
        for spec in self._sections:
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"配置里的 section 缺少 name：{spec!r}")
            text = spec.get("text", "")
            if not isinstance(text, str):
                raise ValueError(f"配置里 section {name!r} 的 text 必须是字符串")
            assembler.section(name, text, order=int(spec.get("order", 100)))
        ctx.provide(PROMPT_SERVICE, assembler)


def prompt_plugin(**config: Any) -> PromptPlugin:
    """工厂：装配单的 `config` 直接喂给构造参数。"""
    return PromptPlugin(**config)


__all__ = ["PROMPT_SERVICE", "PromptPlugin", "prompt_plugin"]


# ======================================================================
# 以下合并自 components/prompt/assembler.py
# ======================================================================


#: section 文本：静态字符串，或按上下文现算。
SectionText = str | Callable[[Mapping[str, Any]], "str | None"]
#: 变量解析器：按上下文算出值。
VariableResolver = Callable[[Mapping[str, Any]], Any]


class PromptError(RuntimeError):
    """提示词装配的错误基类。"""


class DuplicateSectionError(PromptError):
    """同名 section 重复注册（fail loud：要替换先注销）。"""


class UnknownSectionError(PromptError):
    """操作一个不存在的 section（fail loud：防止拼写错误静默生效）。"""


class MissingVariableError(PromptError):
    """section 里用了一个没有来源的变量。"""


@dataclass(frozen=True)
class SectionTrace:
    """一个 section 在这次渲染里的样子（装配单的一项）。"""

    name: str
    order: int
    text: str
    enabled: bool


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


@dataclass
class _Section:
    name: str
    text: SectionText
    order: int
    seq: int
    enabled: bool = True


#: 变量占位符：`{{name}}`（允许两侧空白）。
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


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
            text = _VARIABLE_PATTERN.sub(
                lambda match, where=section.name: _substitute(
                    match, where, context, self._variables, used
                ),
                raw,
            )
            traces.append(
                SectionTrace(name=section.name, order=section.order, text=text, enabled=True)
            )
        body = "\n\n".join(
            trace.text.strip() for trace in traces if trace.enabled and trace.text.strip()
        )
        return RenderedPrompt(text=body, sections=tuple(traces), variables=used)


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


__all__ = [
    "DuplicateSectionError",
    "MissingVariableError",
    "PromptAssembler",
    "PromptError",
    "RenderedPrompt",
    "SectionText",
    "SectionTrace",
    "UnknownSectionError",
    "VariableResolver",
]
