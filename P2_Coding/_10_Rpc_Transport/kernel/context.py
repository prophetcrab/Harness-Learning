"""Context —— 插件共享的注册中心：注册即 effect，卸载自动回卷（铁律 #3）。

M6 的第一个机制（`_07` 唯一新增的东西）。它回答的问题是：
"一堆注册动作（塞工具、装能力、记配置）——怎么做到**整体卸载、干净回卷**？"

三条设计：

1. **一切注册都是 effect**：`ctx.effect(name, install)` 里的 `install` 返回的函数
   （disposer）会登记为"撤销动作"；而且 **`provide` 在装载期会自动登记撤销**
   （注册即 effect 的字面落实）——插件崩了、effect 卸载了，登记物都会被回卷。
   `ctx.unload(name)` 卸载该 effect 时**自动按后进先出（LIFO）顺序**执行 disposer。
   注册与撤销成对出现，谁也不用记"我装过什么"。

2. **effect 成树**：装载期（install 执行中）注册的 effect 会成为当前 effect 的
   **子 effect**；卸载父 effect 时，子 effect **先**逐层回卷（深的先撤、同级后进
   先撤），再轮到父自己的 disposer。这让"插件 setup 里又注册了几个小 effect"这种
   嵌套结构能整体、按正确顺序拆干净（对应 Cordis："If teardown order matters,
   keep the related work in one effect so disposal unwinds in the intended
   sequence"）。

3. **注册物按名字归档，重复注册 fail loud**：`ctx.provide(key, value)` 把供体
   登记的产物放进命名槽位（如 "fs"、"tool:shell"）；同名重复直接报错（铁律 #8：
   装配错误启动时立刻炸，不静默覆盖）。

**回卷纪律**：`unload(name)` 逆序执行 disposer；某个 disposer 抛异常时——
**继续回卷其余部分**，最后把第一个异常抛出去（半装状态比报错更糟）。

对应 dsh：`docs/cordis-primer.md` 的 "Registrations are reversible effects"
（`ctx.effect()` 返回 disposer）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# install：装载函数，可返回 disposer（Callable[[], None]）或 None（无撤销动作）。
Install = Callable[[], Callable[[], None] | None]
Disposer = Callable[[], None]


class DuplicateSlotError(ValueError):
    """同名槽位/effect 重复注册（fail loud，不静默覆盖）。"""


class UnknownEffectError(KeyError):
    """卸载一个不存在的 effect（fail loud，防止拼写错误静默生效）。"""


@dataclass
class _Effect:
    """一个已装载（或正在装载）的 effect：名字 + disposer + 子 effect + 父引用。"""

    name: str
    disposers: list[Disposer] = field(default_factory=list)
    children: list[_Effect] = field(default_factory=list)
    parent: _Effect | None = None


class Context:
    """插件共享的注册中心（ctx）。

    用法（插件视角）：

        def setup(ctx: Context):
            ctx.provide("greeting", "你好")
            return lambda: ctx.retract("greeting")   # 显式撤销动作

    用法（装载器视角）：

        ctx = Context()
        dispose = ctx.effect("greeting-plugin", lambda: setup(ctx))
        ...
        dispose()                      # 或 ctx.unload("greeting-plugin")

    嵌套：装载中再次调用 effect/register，新 effect 自动挂到当前 effect 之下。
    """

    def __init__(self) -> None:
        self._slots: dict[str, Any] = {}
        self._effects: dict[str, _Effect] = {}   # 名字 → effect（含子 effect）
        self._stack: list[_Effect] = []           # 正在装载的 effect 栈（父子关系用）
        self._auto = 0                            # register() 的自动编号

    # ------------------------------------------------------------------
    # 命名槽位（供体登记产物）
    # ------------------------------------------------------------------

    def provide(self, key: str, value: Any) -> None:
        """把一个产物登记进命名槽位；同名重复 → DuplicateSlotError（fail loud）。

        **注册即 effect**：若在某个 effect 的装载期调用（install 执行中），本次
        登记会自动挂上"撤销槽位"的 disposer——插件崩了、effect 卸载了，槽位都会
        被回卷，不需要插件作者额外写撤销动作。effect 外直接调用则不自动登记
        （此时由调用方自己管理，如测试脚手架）。
        """
        if key in self._slots:
            raise DuplicateSlotError(f"槽位已存在：{key}（重复注册直接报错，不静默覆盖）")
        self._slots[key] = value
        if self._stack:  # 装载期：自动登记的撤销动作（幂等 pop，防与显式撤销重复）
            self._stack[-1].disposers.append(lambda: self._slots.pop(key, None))

    def retract(self, key: str) -> Any:
        """撤销一个槽位并返回原值；不存在 → KeyError（fail loud）。"""
        if key not in self._slots:
            raise KeyError(f"槽位不存在：{key}（无法撤销）")
        return self._slots.pop(key)

    def get(self, key: str, default: Any = None) -> Any:
        """取一个槽位；缺省返回 default（消费方通常应显式处理缺失）。"""
        return self._slots.get(key, default)

    def require(self, key: str) -> Any:
        """取一个槽位；缺失 → KeyError（消费方在"必须有"时用这个）。"""
        if key not in self._slots:
            raise KeyError(f"槽位未提供：{key}（检查对应的插件是否已装载）")
        return self._slots[key]

    @property
    def slots(self) -> list[str]:
        """当前登记的槽位名（按登记顺序）。"""
        return list(self._slots)

    # ------------------------------------------------------------------
    # effect：注册即回卷单元
    # ------------------------------------------------------------------

    def effect(self, name: str, install: Install) -> Disposer:
        """装载一个 effect：执行 install，登记它返回的 disposer。

        返回一个**卸载函数**（调用即回卷本 effect——含它的子 effect——并注销台账）。
        name 重复 → DuplicateSlotError（同名 effect 不共存，避免两份台账）。

        装载期（本 effect 的 install 执行中）注册的其它 effect 自动成为子 effect：
        卸载时按"子先父后、同级后进先出"回卷。
        """
        if name in self._effects:
            raise DuplicateSlotError(f"effect 已存在：{name}（先 unload 再重装）")
        record = _Effect(name=name, parent=self._stack[-1] if self._stack else None)
        self._effects[name] = record
        if record.parent is not None:
            record.parent.children.append(record)

        self._stack.append(record)
        try:
            disposer = install()
        except BaseException:
            # 装载失败：把已登记的（含子 effect）撤销掉再抛——不留半装状态。
            self._stack.pop()
            self._detach(record)
            self._forget(record)
            self._unwind(record)
            raise
        self._stack.pop()
        if disposer is not None:
            record.disposers.append(disposer)
        return lambda: self.unload(name)

    def register(self, install: Install) -> Disposer:
        """平铺版 effect：每次调用自动编号一个独立 effect（不关心名字时用）。"""
        self._auto += 1
        return self.effect(f"auto:{self._auto}", install)

    def unload(self, name: str) -> None:
        """卸载一个 effect：先逐层回卷子 effect，再逆序执行自己的 disposer。

        某个 disposer 抛异常时：**继续回卷其余部分**，最后把第一个异常抛出去。
        """
        record = self._effects.get(name)
        if record is None:
            raise UnknownEffectError(f"effect 不存在：{name}（无法卸载）")
        self._detach(record)   # 先与父链断开：父之后卸载时不会重复回卷它
        self._forget(record)   # 从台账摘掉整棵子树（不动 children 结构——回卷要用）
        errors = self._unwind(record)
        if errors:
            raise errors[0]

    @property
    def effect_names(self) -> list[str]:
        """当前装载的 effect 名（按装载顺序）。"""
        return list(self._effects)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _detach(record: _Effect) -> None:
        """把 effect 从父的 children 列表里摘掉（父链断开，防重复回卷）。"""
        if record.parent is not None and record in record.parent.children:
            record.parent.children.remove(record)

    def _forget(self, record: _Effect) -> None:
        """从台账里摘掉一个 effect（连同它的子树）。

        只动 `self._effects` 这张名字表：**不碰 children 结构**——回卷
        （_unwind）要靠它逐层展开子 effect。
        """
        self._effects.pop(record.name, None)
        for child in record.children:
            self._forget(child)

    @staticmethod
    def _unwind(record: _Effect) -> list[BaseException]:
        """回卷：子 effect 先（后进先出），然后自己的 disposer（后进先出）。

        返回途中捕获的异常（不错过其余回卷）。
        """
        errors: list[BaseException] = []
        while record.children:
            child = record.children.pop()  # 同级后进先出
            errors.extend(Context._unwind(child))
        while record.disposers:
            disposer = record.disposers.pop()  # LIFO：后进先出
            try:
                disposer()
            except BaseException as exc:  # noqa: BLE001 —— 全部捕获后统一重抛
                errors.append(exc)
        return errors


__all__ = ["Context", "DuplicateSlotError", "UnknownEffectError", "Install", "Disposer"]
