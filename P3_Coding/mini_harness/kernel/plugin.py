"""Plugin —— 插件协议与声明（name / inject / optional / category / setup）。

一个插件就是"有 `setup(ctx)` 方法的对象"，或（更轻）一个普通函数/可调用对象
——沿用 P2 `_07` 的鸭子类型协议。P3 新增两样**声明**（`_05` 补了第三样）：

- `name`：插件身份（缺失时用函数名 / 类限定名兜底）；
- `inject`：**必需**依赖的服务名列表（缺失则插件保持 pending，等服务就绪自动
  激活；依赖消失则自动停用）——**装载顺序无关**，依赖关系驱动生命周期；
- `optional`：**可选**依赖的服务名列表。语义与 inject 相对：

  | | 缺依赖时 | 依赖来了 | 依赖走了 | require 取值 |
  |---|---|---|---|---|
  | `inject` | pending，不激活 | 自动激活 | 自动停用 | 一定成功 |
  | `optional` | **照常激活** | **不重装**（靠自己 `ctx.get` 容错） | **不停用** | 可能失败 |

  `optional` 的典型用途是"有则用、没有则降级"，且**不希望因它抖一下就被重启**：
  界面插件就是这样——它想在换模型、装卸插件时**一直开着**，所以对 `agent`
  只能声明成可选，取不到时给用户一个人话错误页，而不是让整个服务消失。

  为什么"不重装"：`optional` 里的服务来来去去不该重启一个正在对外服务的插件
  （重启 = 端口换掉 = 用户眼前的页面失联）。代价是插件要在**用的时候**现取
  （见 plugins/webui.py 的 `_agent_or_none`），而不能在 setup 里抓一份存着。

- `category`：插件在**应用里的角色**（`base` / `capability` / `other`）。
  内核只负责"读出来、记进台账"，不解释它的含义——策略由消费方决定
  （见 plugins/plugin_admin.py：base 不可卸载、其余可按需开关）。
  未声明 → `other`（保守：不声称自己是基础设施）。

协议只需要"有个 setup"，插件作者不必 import 任何基类（与 LLMProvider /
FileSystem / SubprocessService 同款：结构满足即成立）。

本模块只提供"读声明、取 setup"的纯函数；装载与生命周期在 context.py
（Context.load_plugin / unload_plugin）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

# setup：装载函数，可返回 disposer（Callable[[], None]）或 None。
SetupFn = Callable[[Any], "Callable[[], None] | None"]

#: 插件角色的三个取值（`_05` 收尾）：
#:
#:     base        基础：当前应用**不可卸载**的最小集合（卸了应用就不成立）
#:     capability  能力：提供额外能力（工具 / 观测 / 压缩……），可按需开关
#:     other       其他：演示、示例、不参与应用成立与否的插件
#:
#: 内核不解释这三个值的含义——它只负责读出来记进台账；"能不能卸载"这类
#: **策略**由消费方（plugins/plugin_admin.py）决定。这样同一个插件在不同
#: 应用里可以是不同角色（比如聊天应用里 fs 不必是 base）。
PLUGIN_CATEGORIES: tuple[str, ...] = ("base", "capability", "other")
#: 未声明时的默认角色（保守：不声称自己是基础设施）。
DEFAULT_CATEGORY = "other"


@runtime_checkable
class Plugin(Protocol):
    """插件协议：任何有 setup(ctx) 方法的对象都满足它。

    `name` / `inject` / `optional` 是可选声明——运行时用 getattr 读取，
    缺失即默认值。
    """

    def setup(self, ctx: Any) -> Callable[[], None] | None:
        ...


def plugin_name(plugin: object) -> str:
    """插件名：优先 plugin.name；函数用函数名；都没有则用类限定名。"""
    name = getattr(plugin, "name", None)
    if name:
        return str(name)
    if callable(plugin) and not hasattr(plugin, "setup"):
        return str(getattr(plugin, "__name__", type(plugin).__name__))
    return f"{type(plugin).__module__}.{type(plugin).__name__}"


def plugin_setup(plugin: object) -> SetupFn:
    """取出插件的装载函数：有 setup 用 setup，否则把插件本身当函数调用。"""
    setup = getattr(plugin, "setup", None)
    if callable(setup):
        return setup
    if callable(plugin):
        return plugin
    raise TypeError(f"不是合法插件（既没有 setup(ctx) 也不能调用）：{plugin!r}")


def _names(plugin: object, attr: str, *, allow_none: bool = True) -> tuple[str, ...]:
    """读取一个"服务名序列"声明（inject / optional 共用同一套校验）。"""
    raw: Any = getattr(plugin, attr, ())
    if raw is None:
        if allow_none:
            return ()
        raise TypeError(f"插件 {plugin_name(plugin)} 的 {attr} 不能是 None")
    if isinstance(raw, str):
        # 单个字符串是最常见的笔误（会被逐字符迭代）——直接报错。
        raise TypeError(
            f"插件 {plugin_name(plugin)} 的 {attr} 必须是序列（如 ('fs',)），"
            f"收到字符串：{raw!r}"
        )
    if not isinstance(raw, Iterable):
        raise TypeError(f"插件 {plugin_name(plugin)} 的 {attr} 不可迭代：{raw!r}")
    names = tuple(str(item) for item in raw)
    if len(set(names)) != len(names):
        raise ValueError(f"插件 {plugin_name(plugin)} 的 {attr} 有重复项：{names}")
    return names


def plugin_injects(plugin: object) -> tuple[str, ...]:
    """读取插件的**必需**依赖声明（inject）；缺失或非法一律 fail loud。

    接受两种写法：`inject = ("a", "b")`（对象属性）与函数属性 `inject`。
    """
    return _names(plugin, "inject")


def plugin_optionals(plugin: object) -> tuple[str, ...]:
    """读取插件的**可选**依赖声明（optional）：缺了照常激活，不做生命周期管理。

    与 inject 同名冲突直接报错——同一个服务不可能既必需又可选，声明矛盾
    说明作者还没想清楚。
    """
    optionals = _names(plugin, "optional")
    overlap = sorted(set(optionals) & set(plugin_injects(plugin)))
    if overlap:
        raise ValueError(
            f"插件 {plugin_name(plugin)} 的 optional 与 inject 重复：{overlap}"
            f"（一个服务要么必需要么可选）"
        )
    return optionals


def plugin_category(plugin: object) -> str:
    """读取插件的角色声明（category）；缺失取默认值，非法值 fail loud。

    为什么非法值要报错而不是回退：写错一个字母（`"capabilty"`）会让这个插件
    悄悄降级成"其他"、失去它本该有的行为（比如被当成可卸载的）。宁可装载期炸。
    """
    raw = getattr(plugin, "category", DEFAULT_CATEGORY)
    if raw is None:
        return DEFAULT_CATEGORY
    value = str(raw)
    if value not in PLUGIN_CATEGORIES:
        raise ValueError(
            f"插件 {plugin_name(plugin)} 的 category 不合法：{value!r}"
            f"（可用：{'、'.join(PLUGIN_CATEGORIES)}）"
        )
    return value


__all__ = [
    "DEFAULT_CATEGORY",
    "PLUGIN_CATEGORIES",
    "Plugin",
    "SetupFn",
    "plugin_category",
    "plugin_injects",
    "plugin_name",
    "plugin_optionals",
    "plugin_setup",
]
