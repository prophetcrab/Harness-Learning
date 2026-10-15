"""插件声明纪律 —— 用测试把"约定"变成"不变量"。

**为什么需要它**：插件之间只通过服务名协作，而"我用了谁"这件事原先只写在
代码里（`ctx.get(...)` / `ctx.require(...)`），`inject` / `optional` 声明是
**手写**的。手写就会漏，漏了还不会响——这次实测就抓到一个：`token-usage`
用 `ctx.get(TOOLS_SERVICE)` 注册工具却**没声明**，于是换掉 tools 插件时
内核不知道要重启它，`usage_stats` 从工具面上永久消失（换个插件，工具没了）。

漏声明的后果按取值方式分两种：

    ctx.require(x) 而没声明 → 装载期就报错（fail loud，能发现）
    ctx.get(x)     而没声明 → **静默**：依赖变了不会被重启，
                              而且"当前缺什么"在台账里也看不见

所以本文件盯着第二种。规则一句话：

    **凡是用 `ctx.get` / `ctx.require` 取过的服务，都必须出现在该插件的
    `inject`（必需）或 `optional`（可选）声明里。**

怎么查：AST 扫源码找出"取了哪些服务"（比运行期更严格——等待中的插件不会
执行 setup，跑 setup 就查不全），再把插件类上的声明取出来做包含判断。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

#: 不是插件的模块（词汇表 / 装配入口 / 包导出）。
NOT_PLUGINS = {"__init__.py", "boot.py", "events.py", "services.py"}


def _plugin_modules() -> list[str]:
    return sorted(
        path.stem for path in PLUGINS_DIR.glob("*.py") if path.name not in NOT_PLUGINS
    )


def _used_services(module: object, source: str) -> set[str]:
    """AST 扫出模块里 `ctx.get(X)` / `ctx.require(X)` 用到的服务名。

    X 既可能是字符串字面量（`ctx.get("tools")`），也可能是常量名
    （`ctx.get(TOOLS_SERVICE)`）——后者按模块命名空间求值。

    receiver 只认 `ctx` / `self._ctx`：这两个才是 Context 的引用；
    `settings.get("k")` 之类的同名调用不该被误报。
    """
    namespace = vars(module)
    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("get", "require"):
                receiver = func.value
                is_context = (isinstance(receiver, ast.Name) and receiver.id == "ctx") or (
                    isinstance(receiver, ast.Attribute) and receiver.attr == "_ctx"
                )
                if is_context and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
                    elif isinstance(arg, ast.Name) and isinstance(
                        namespace.get(arg.id), str
                    ):
                        found.add(namespace[arg.id])
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return found


def _declared_services(module: object) -> tuple[set[str], set[str]]:
    """模块里所有插件类声明的 (inject, optional) 并集。

    一个模块可能导出多个插件类（或工厂函数返回的类），所以取并集——
    判据是"这个模块用到的服务有没有人声明"。
    """
    injects: set[str] = set()
    optionals: set[str] = set()
    for value in vars(module).values():
        if not isinstance(value, type):
            continue
        raw_inject = getattr(value, "inject", ())
        raw_optional = getattr(value, "optional", ())
        if isinstance(raw_inject, (tuple, list)):
            injects.update(str(item) for item in raw_inject)
        if isinstance(raw_optional, (tuple, list)):
            optionals.update(str(item) for item in raw_optional)
    return injects, optionals


def _cases() -> list[tuple[str, set[str], set[str], set[str]]]:
    rows = []
    for name in _plugin_modules():
        module = importlib.import_module(f"plugins.{name}")
        source = (PLUGINS_DIR / f"{name}.py").read_text(encoding="utf-8")
        injects, optionals = _declared_services(module)
        used = _used_services(module, source)
        rows.append((name, used, injects, optionals))
    return rows


def test_plugin_modules_are_discovered():
    """自查：别因为路径写错而"零个插件全绿"。"""
    modules = _plugin_modules()
    assert "token_usage" in modules and "webui" in modules and "agent" in modules
    assert len(modules) >= 12, f"只扫到 {len(modules)} 个插件模块，路径可能不对"


@pytest.mark.parametrize("name,used,injects,optionals", _cases())
def test_every_used_service_is_declared(
    name: str, used: set[str], injects: set[str], optionals: set[str]
) -> None:
    """**通过测试钉住的约定**：取过的服务必须在 inject 或 optional 里声明。

    这条测试的价值不在"现在绿"，而在**以后每个新插件都躲不过**——
    漏声明会当场变红，而不是等到某个插件被换掉时才发现东西没了。
    """
    undeclared = sorted(used - injects - optionals)
    assert not undeclared, (
        f"插件 {name} 用了这些服务但没声明：{undeclared}\n"
        f"  用的：{sorted(used)}\n"
        f"  inject={sorted(injects)}  optional={sorted(optionals)}\n"
        "  必须声明的原因：内核靠 inject/optional 决定"
        "『依赖变化时要不要重启这个插件』；漏声明的后果是静默的——"
        "依赖被换掉时它不会被重启，注册过的东西（工具等）就永久消失。"
    )


@pytest.mark.parametrize("name,used,injects,optionals", _cases())
def test_required_and_optional_do_not_overlap(
    name: str, used: set[str], injects: set[str], optionals: set[str]
) -> None:
    """同一个服务不能又必需又可选（声明矛盾，内核也会报错——这里提前拦）。"""
    assert not (injects & optionals), f"插件 {name} 同时 inject 和 optional 了：{injects & optionals}"
