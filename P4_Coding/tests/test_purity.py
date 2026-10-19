"""步 8 验收：纯度 —— 基座不认识 harness，框架不认识插件。

对应 PLAN.md「M1 拆步」第 8 步与决策 0042 的三条判据：

    1. 除基座外没有非插件部分
    2. 装卸不动别人（插件之间只有服务与事件）
    3. 基座自足且不懂 harness

扫描口径（刻意写清楚，避免"扫了个寂寞"）：

    · 只看 **Python 代码**：AST 里的 import、标识符、字符串常量；
      文档字符串（module / class / function 的 docstring）与注释不算——
      它们本来就是用来解释边界的，出现"llm 这类词"是说明而不是能力；
    · 词表按**完整单词**匹配（llm / tool / session / agent / prompt / web），
      所以 `promptly`、`database` 这类不会误伤；
    · 框架 = core/context.py + core/plugin.py；引导 = core/launcher.py；
      包门面 = core/__init__.py（允许导出引导的 API）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "core"

#: 框架模块：插件世界的"物理定律"。
FRAMEWORK = ("context.py", "plugin.py")
#: 引导模块：唯一允许认识 loader 的地方。
LAUNCHER = "launcher.py"
#: 包门面：导出框架 + 引导的公开 API。
FACADE = "__init__.py"

#: harness 的领域词汇——框架与引导都不许在代码里出现。
HARNESS_WORDS = ("llm", "tool", "session", "agent", "prompt", "web")
#: 三类角色的词表——**只允许引导知道**（0041 的产品概念）。
ROLE_WORDS = ("base", "application", "capability")


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------


def _parse(name: str) -> ast.Module:
    return ast.parse((CORE / name).read_text(encoding="utf-8"), filename=str(CORE / name))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """所有 docstring 的 Constant 节点 id（扫描时排除）。"""
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", ())
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                nodes.add(id(body[0].value))
    return nodes


def _code_words(tree: ast.Module) -> list[str]:
    """代码里出现过的"词"：import 的模块名、标识符、字符串常量（docstring 除外）。"""
    docstrings = _docstring_nodes(tree)
    words: list[str] = []

    def add(text: str) -> None:
        # 标识符按 _ 与 . 切开（prompt_service / web.ui 也算命中）
        for chunk in text.replace(".", "_").split("_"):
            if chunk:
                words.append(chunk.lower())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                add(node.module)
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.Name):
            add(node.id)
        elif isinstance(node, ast.Attribute):
            add(node.attr)
        elif isinstance(node, ast.arg):
            add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                add(node.value)
    return words


def _top_level_imports(tree: ast.Module) -> list[str]:
    """模块顶层的 import 目标（不含函数体内的懒加载）。"""
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module)
                found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _imports_plugins(tree: ast.Module) -> bool:
    return any(name == "plugins" or name.startswith("plugins.") for name in _top_level_imports(tree))


# ---------------------------------------------------------------------------
# 判据 3a：框架不认识插件
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FRAMEWORK + (FACADE,))
def test_framework_has_no_top_level_plugins_import(name: str):
    assert not _imports_plugins(_parse(name)), (
        f"core/{name} 在顶层 import 了 plugins——框架不能认识任何插件"
    )


def test_launcher_may_know_plugins_only_lazily():
    """引导是唯一的组合根：它可以 import plugins，但必须是函数内懒加载。"""
    tree = _parse(LAUNCHER)
    assert not _imports_plugins(tree), "core/launcher.py 的 plugins import 必须放在函数体内"
    # 证明它确实认识 loader（不是碰巧没写）
    assert "plugins" in (CORE / LAUNCHER).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 判据 3b：框架不懂 harness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FRAMEWORK + (LAUNCHER,))
def test_no_harness_vocabulary_in_code(name: str):
    words = set(_code_words(_parse(name)))
    hits = sorted(words & set(HARNESS_WORDS))
    assert not hits, f"core/{name} 的代码里出现了 harness 词汇：{hits}"


# ---------------------------------------------------------------------------
# 两级纪律：角色词表只允许在引导里
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FRAMEWORK)
def test_role_vocabulary_is_absent_from_framework(name: str):
    words = set(_code_words(_parse(name)))
    hits = sorted(words & set(ROLE_WORDS))
    assert not hits, (
        f"core/{name} 出现了角色词 {hits}——三类角色是 P4 的产品概念，只有引导能知道"
    )


def test_launcher_owns_the_role_vocabulary():
    source = (CORE / LAUNCHER).read_text(encoding="utf-8")
    for word in ROLE_WORDS:
        assert word in source, f"引导应当声明角色词 {word!r}（产品政策在它这里）"


# ---------------------------------------------------------------------------
# 判据 3c：基座自足（空装配单能起）——行为验收见 test_launcher.py
# ---------------------------------------------------------------------------


def test_core_package_imports_without_plugins_loaded():
    """`import core` 不会把 plugins 包拖进来（基座自足）。"""
    import subprocess
    import sys

    code = (
        "import sys; import core; "
        "assert 'plugins' not in sys.modules, sorted(k for k in sys.modules if 'plugin' in k); "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=HERE,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
