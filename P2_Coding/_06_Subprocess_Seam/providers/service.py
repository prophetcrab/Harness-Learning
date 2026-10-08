"""ServiceContainer —— 单槽能力容器：一个能力名，只容纳一个 provider。

铁律 #5 说"接缝 = Definition + Provider + Consumer，**单槽服务重复注册直接报错**"；
铁律 #6 说"**显式解析优于隐式默认**"——默认值在显式的 resolve 步骤给出，
不藏在执行函数里。本模块就是把这两条落成一个几十行的小对象：

    services = ServiceContainer()
    services.register("fs", LocalFS(workspace))   # 登记一次；重复登记 → 报错
    fs = services.resolve("fs")                   # ★ 显式解析点：谁在这拿、用什么
    registry = build_filesystem_registry(fs)      # 消费者只认协议，不知具体是谁

为什么是"单槽"而不是"多槽列表"：一个能力同时只有一个"当前实现"，
多实现并存会让"到底在用哪个"变得隐蔽；想换实现就换一个注册进去
（未来加了卸载/回卷就是 _07 的插件 effect，这里先保持最小）。
"""

from __future__ import annotations

from typing import Any

# 能力的槽位名（工具与接线都用它 resolve，避免各处拼字符串）。
# 每个能力单槽：同一能力重复注册报错；不同能力各占一槽（容器可装多个能力）。
FS_CAPABILITY = "fs"
SUBPROCESS_CAPABILITY = "subprocess"


class ServiceContainer:
    """极简能力容器：register 一次、resolve 一次、使用到处。"""

    def __init__(self) -> None:
        self._slots: dict[str, Any] = {}

    def register(self, capability: str, provider: Any) -> None:
        """登记一个能力的实现。该能力已有实现 → 直接报错（fail loud，不静默顶替）。"""
        if capability in self._slots:
            raise ValueError(
                f"能力 {capability} 已注册（单槽服务：先注销再换，或换一个容器）"
            )
        self._slots[capability] = provider

    def resolve(self, capability: str) -> Any:
        """显式解析一个能力的实现；未注册 → 直接报错（不返回 None、不给隐式默认）。"""
        if capability not in self._slots:
            raise KeyError(f"能力 {capability} 未注册：请先 register 再 resolve")
        return self._slots[capability]

    def has(self, capability: str) -> bool:
        """该能力是否已登记（查询用；正常路径应直接 resolve 并处理报错）。"""
        return capability in self._slots

    @property
    def capabilities(self) -> list[str]:
        """已登记的能力名（按登记顺序）。"""
        return list(self._slots)


__all__ = ["ServiceContainer", "FS_CAPABILITY", "SUBPROCESS_CAPABILITY"]
