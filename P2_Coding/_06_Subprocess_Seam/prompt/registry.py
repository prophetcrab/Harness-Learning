"""SectionRegistry —— 有序的 section 注册表，带"作用域遮蔽"。

M4 的第一个机制：把提示词从"一个字符串"变成"一张有序注册表 + 一层可选的作用域覆盖"。

两层结构（与 harness/tools/registry.py 的工具注册表同构，都是在落实铁律 #4）：

    基础层（base）   顺序固定，是一个会话的"默认提示词构成"
    作用域层（scope） 叠加在基础层之上：同名则**遮蔽**（most-specific-wins），
                     未同名则**追加**在末尾；作用域撤下后，被遮蔽的基础层 section
                     自动重新可见。

为什么是"遮蔽"而不是"删除"：基础层是共享的，任何局部定制都不应该篡改它。作用域
只在解析时挡住同名项，基础层那条始终还在——这样"撤下作用域"就是无副作用的回卷。

排序规则（很重要，必须在装配时稳定）：
1. 基础 section 按**注册顺序**；
2. 作用域覆盖一个已存在的名字时，**保持它在基础层的位置**（原地替换，不打乱顺序）；
3. 作用域新增一个基础层没有的名字时，**追加到全部基础 section 之后**（按域内注册顺序）。

重名策略（与工具注册表保持一致）：
- 基础层：重复注册同名 → 直接报错（fail loud，绝不静默覆盖）；
- 作用域层：重复注册同名 → 允许（同层内的"就地覆盖"，方便反复调整）。

注意：本模块不负责"把 section 拼成文本"——那是 assembler.py 的事。这里只负责
"最终有哪些 section、按什么顺序"。
"""

from __future__ import annotations

from prompt.section import Section


class SectionRegistry:
    """基础层的 section 注册表；可派生作用域覆盖层。"""

    def __init__(self) -> None:
        # dict 自 3.7 起保持插入顺序，正好用来实现"注册顺序 = 装配顺序"。
        self._base: dict[str, Section] = {}

    # ------------------------------------------------------------------
    # 基础层：注册 / 注销 / 查询
    # ------------------------------------------------------------------

    def register(self, section: Section) -> None:
        """注册一节到基础层。重复名字直接报错（fail loud，不静默覆盖）。

        想"替换"基础层某一节，正确做法是派生一个作用域、在作用域里注册同名 section——
        那是遮蔽，不是覆盖，基础层不受影响。
        """
        if section.name in self._base:
            raise ValueError(
                f"基础层 section 重名：{section.name}（如需替换请用 registry.scoped(...) 遮蔽）"
            )
        self._base[section.name] = section

    def unregister(self, name: str) -> Section:
        """从基础层移除一节并返回它；名字不存在则报错。"""
        if name not in self._base:
            raise KeyError(f"基础层不存在 section：{name}")
        return self._base.pop(name)

    def get(self, name: str) -> Section | None:
        """按名字取基础层的一节（不存在返回 None）。"""
        return self._base.get(name)

    @property
    def names(self) -> list[str]:
        """基础层的名字，按注册顺序。"""
        return list(self._base)

    def sections(self) -> list[Section]:
        """基础层的全部 section，按注册顺序（返回的是新列表，外部改动不影响内部）。"""
        return list(self._base.values())

    # ------------------------------------------------------------------
    # 派生作用域
    # ------------------------------------------------------------------

    def scoped(self, scope_name: str) -> SectionScope:
        """派生一个作用域覆盖层（读穿透到基础层，写/遮蔽只影响本层）。"""
        return SectionScope(self, scope_name)


class SectionScope:
    """一个作用域覆盖层：同名遮蔽基础层，未同名则追加（most-specific-wins）。

    遮蔽发生在**解析时**，不是注册时删除：基础层的同名 section 仍留在基础层，
    只是被本层的定义挡住；`close()`（或 `drop()`）之后基础层定义自动重新可见。
    """

    def __init__(self, parent: SectionRegistry, name: str) -> None:
        self._parent = parent
        self._name = name
        # 本层定义：名字 → section（同层可覆盖）。
        self._local: dict[str, Section] = {}
        # close() 之后本层不再生效，视同不存在。
        self._closed = False

    @property
    def scope_name(self) -> str:
        return self._name

    # ---- 本层写操作 ----

    def register(self, section: Section) -> None:
        """注册/覆盖一节到本作用域（同名覆盖本层旧定义是允许的）。"""
        self._ensure_open()
        # 打上来源标签，便于后续追溯"这一节来自哪个作用域"（如果调用方没写来源）。
        # 注意：不改动传入对象，而是换一个带来源的副本，保持 Section 的不可变性。
        if section.source == "builtin":
            section = Section(
                name=section.name,
                content=section.content,
                source=self._name,
                title=section.title,
            )
        self._local[section.name] = section

    def drop(self, name: str) -> None:
        """撤销本层对某一节的（遮蔽或新增）定义；基础层若存在同名则重新可见。"""
        self._ensure_open()
        self._local.pop(name, None)

    def close(self) -> None:
        """撤下整个作用域：本层全部定义失效，基础层完全恢复。"""
        self._closed = True
        self._local.clear()

    # ---- 本层读操作（合并视图） ----

    def sections(self) -> list[Section]:
        """合并视图：基础层顺序为骨架，本层同名项就地替换，本层新增项追加到末尾。"""
        base = self._parent.sections()
        if self._closed:
            return base

        base_names = {s.name for s in base}
        # 1) 基础层每一节：本层有同名则用本层的（保持原位），否则用基础层的。
        merged = [self._local.get(s.name, s) for s in base]
        # 2) 本层新增（基础层没有的）→ 追加，按本层注册顺序。
        merged.extend(s for name, s in self._local.items() if name not in base_names)
        return merged

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.sections()]

    # ---- 内部 ----

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"作用域已关闭：{self._name}（close() 之后不可再写入）")


__all__ = ["SectionRegistry", "SectionScope"]
