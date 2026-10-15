"""loader —— 把**一段插件源码**变成一个可装载的插件工厂（纯机制，无策略）。

**为什么值得单独一个模块**（`_06`）：这件事原先埋在 `plugins/self_tools.py`
里（`_plugin_from_source`），是"模型安装新插件"那条路的私有细节。到了 `_06`,
**装一个工具插件**同样要点这一下火（写一个 `.py` → 装载 → 工具出现在工具面），
而"工具插件"是独立成插件的一类东西——再让它们共享一个藏在自扩展插件里的
私有函数，就等于说"没有 self-tools 就不能装工具插件"。

放内核里还有个更硬的理由：**它是纯机制**——"源码 → 模块 → 插件对象"这件事与
harness 的业务毫无关系，不依赖任何服务，也不认识任何插件名（判据与
`kernel/plugin.py` 读声明那组纯函数一致）。策略（白名单、审批、从哪读源码）
留在插件层。

**入口约定**（与项目里的插件写法一致，三种认一个就行）：

    def factory(): return Plugin()            # 通用
    def <文件名>_plugin(): ...                # 与本项目各 plugins/*.py 同款
    class <唯一一个带 setup 的类>              # 兜底：自动实例化

`entry` 显式给了就先用它。

**安全边界不在本模块**：它不管"这段代码能不能跑"——那是审批（人看过源码）
与白名单（只装批准过的名字）的事。本模块只负责把源码变成对象，并把
"找不到入口 / 语法错误 / 导入失败"如实变成异常，由调用方转成人话回给模型或人。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


def _constant(instance: object) -> Callable[[], object]:
    """把一个已存在的插件实例包成工厂（重装时"还给你这一个"，见下）。

    源码里直接把实例挂在模块级（`plugin = MyPlugin()`）时，重装语义只能是
    "还给你这一个"——它不是可复制的声明，而是一次构造的产物。这是刻意的降级：
    工厂缺失不报错，但行为被如实限住。
    """

    def factory() -> object:
        return instance

    return factory


def _needs_instantiation(obj: object) -> bool:
    """这个入口是"要调用一次才得到插件"的吗（工厂函数 / 插件类）？

    **判据是"有没有 setup"，不是"可不可调用"**——这条是实测挖出来的 bug：
    原先写的是 `callable(obj) and not hasattr(obj, "setup")`，于是

        class WordCountPlugin:          # 类本身有 setup（方法）
            def setup(self, ctx): ...   # → hasattr(cls, "setup") 为 True

    被当成"已经是实例"，类对象直接交给内核 → 装载时报
    `setup() missing 1 required positional argument: 'ctx'`（模型按 entry=
    "WordCountPlugin" 指定类名时必然踩到，而它是最自然的写法）。

    正确判据：**类**（type 的实例）一定需要实例化——不管它有没有 setup；
    非类的可调用对象（工厂函数）同样需要调用；只有真正的插件**实例**不用。
    """
    if isinstance(obj, type):
        return True
    return callable(obj) and not hasattr(obj, "setup")


def plugin_from_source(
    source: str,
    *,
    path: str = "plugin.py",
    entry: str | None = None,
) -> tuple[object, Callable[[], object], str]:
    """把一段源码编译成插件，返回 `(插件实例, 工厂, 插件名)`。

    工厂是"再造一个同款插件"的能力（重装/多实例要用）：`load_plugin` 卸载后
    再装载的场景里，注册表里留着的就是它。

    实现细节：写进**临时文件**再按文件路径导入——源码来自工作区，用文件加载
    最直观（`__file__`、相对导入的报错信息都可读）；模块名带 `user_plugin_`
    前缀，避免撞上本进程里已有的模块（它不会被当成本项目的插件包）。
    """
    stem = Path(path).stem
    module_name = f"user_plugin_{stem}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", prefix=f"{module_name}_", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(source)
        tmp_path = handle.name
    try:
        spec = importlib.util.spec_from_file_location(module_name, tmp_path)
        if spec is None or spec.loader is None:
            raise ValueError("无法为该文件建立模块规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    from kernel.plugin import plugin_name

    candidates: list[str] = []
    if entry:
        candidates.append(entry)
    candidates += ["factory", f"{stem}_plugin"]
    candidates += ["plugin", "PLUGIN"]

    for attr in candidates:
        obj = getattr(module, attr, None)
        if obj is None:
            continue
        if _needs_instantiation(obj):
            # 工厂函数 / 插件类：调用它得到实例（类也是"每次调用造一个新的"）
            instance = obj()
            factory: Callable[[], object] = obj
        else:
            instance = obj  # 已经是插件实例（模块里的 plugin / PLUGIN 常量）
            factory = _constant(instance)
        if hasattr(instance, "setup"):
            return instance, factory, plugin_name(instance)

    classes = [
        value
        for value in vars(module).values()
        if isinstance(value, type) and hasattr(value, "setup")
    ]
    if len(classes) == 1:
        cls = classes[0]
        instance = cls()
        return instance, cls, plugin_name(instance)

    raise ValueError(
        "找不到插件入口：请在源码里定义 factory()、"
        f"{stem}_plugin()，或唯一一个带 setup 方法的类"
    )


__all__ = ["plugin_from_source"]
