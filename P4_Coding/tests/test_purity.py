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


# ---------------------------------------------------------------------------
# 层纪律：只有三层（core / plugins / 入口），不再有 components
# ---------------------------------------------------------------------------


def test_no_components_layer():
    """项目只有 core 与 plugins 两层代码（加入口脚本）。

    共享类型与纯函数按**功能域**并进对应插件模块（如 `plugins/base/tools_admin.py`
    里的 `@tool` / `ToolDefinition` / `ToolPipeline`）——插件之间可以直接
    import 类型，但**行为协作仍然只有服务与事件两条通道**。没有独立的
    components 层：少一层目录，代价是插件间出现类型 import（已接受）。
    """
    stray = HERE / "components"
    assert not stray.exists(), (
        "components/ 已合并进 plugins/；要加共享类型请放进对应功能域的插件模块"
    )


def test_plugin_type_imports_do_not_form_cycles():
    """插件可以 import 别的插件的类型，但不能形成环形依赖。

    放宽"插件间只能服务与事件"之后，唯一的硬约束是**无环**：类型层可以
    单向引用，一旦成环说明两个模块的职责没有切开。
    """
    plugins = HERE / "plugins"
    modules = {
        path.relative_to(HERE).with_suffix("").as_posix().replace("/", "."): path
        for path in plugins.rglob("*.py")
        if path.name != "__init__.py"
    }
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in _top_level_imports(tree):
            if imported in modules:
                graph[name].add(imported)

    # DFS 找环
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str, stack: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " → ".join([*stack, node])
            raise AssertionError(f"插件间出现环形 import：{cycle}")
        visiting.add(node)
        for neighbour in sorted(graph[node]):
            walk(neighbour, [*stack, node])
        visiting.discard(node)
        visited.add(node)

    for name in sorted(graph):
        walk(name, [])


def _plugin_role(module: str) -> str | None:
    """`plugins.capability.llm.fake` → `capability`；不是插件模块 → None。"""
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == "plugins" and parts[1] in {
        "base",
        "capability",
        "application",
    }:
        return parts[1]
    return None


def test_plugin_imports_go_one_way():
    """插件间 import 只能**由外向内**：capability → base 允许，反向禁止。

    这是"最小耦合"的可执行版本（决策 0053）：

        · base 是稳定层，不认识任何具体能力；
        · capability 可以用 base 的类型 / 纯函数（身份一致才不丢 isinstance）；
        · **capability 之间不 import**——兄弟能力要么走服务与事件，要么各自
          留一份副本（重复可以接受，隐式依赖不可以）；
        · application 是组合根，可以认识两者。

    行为协作仍然只有服务与事件两条通道；这条规则约束的是**类型层**。
    """
    plugins = HERE / "plugins"
    violations: list[str] = []
    for path in plugins.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        module = path.relative_to(HERE).with_suffix("").as_posix().replace("/", ".")
        source_role = _plugin_role(module)
        if source_role is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in _top_level_imports(tree):
            target_role = _plugin_role(imported)
            if target_role is None:
                continue
            if source_role == "base" and target_role in {"capability", "application"}:
                violations.append(f"{module}（base）→ {imported}（{target_role}）")
            elif source_role == "capability" and target_role == "capability":
                violations.append(f"{module}（capability）→ {imported}（兄弟能力）")
            elif source_role == "capability" and target_role == "application":
                violations.append(f"{module}（capability）→ {imported}（application）")
    assert violations == [], (
        "插件间 import 方向不对（只允许 capability → base / application → 任意）：\n  "
        + "\n  ".join(violations)
    )
# ---------------------------------------------------------------------------
# 插件目录按默认角色分类（0041 / 0035）
# ---------------------------------------------------------------------------


def test_plugins_live_in_role_directories():
    """每个插件模块都必须在 base/ capability/ application/ 之下。

    顶层 `plugins/*.py` 只允许包门面（`__init__.py`）——否则"三类插件"的
    物理归类会随着新插件慢慢烂掉，装配单里的角色也失去默认值。
    """
    allowed_top = {"__init__.py"}
    stray = [
        path.name
        for path in (HERE / "plugins").glob("*.py")
        if path.name not in allowed_top
    ]
    assert stray == [], (
        f"插件模块必须放进 plugins/<角色>/ 下（base / capability / application）：{stray}"
    )

    role_dirs = {"base", "capability", "application"}
    present = {path.name for path in (HERE / "plugins").iterdir() if path.is_dir()}
    unexpected = sorted(present - role_dirs - {"__pycache__"})
    assert unexpected == [], f"plugins/ 下出现了未知目录：{unexpected}"


#: capability 的二级目录（按功能域分组）；加新组时同步这张表。
CAPABILITY_GROUPS = {
    "llm", "prompt", "session", "fs", "proc", "workspace", "tools", "approval", "agent",
}


def test_capability_plugins_live_in_domain_directories():
    """能力插件必须放进功能域子目录——顶层只允许包门面。

    目录只是分组，插件身份仍是各自的 `name`（如 `tool-fs`），与目录无关；
    但"平铺一层"会让能力层随着插件增多变得不可读（十几个文件谁属于谁全靠猜）。
    """
    capability = HERE / "plugins" / "capability"
    stray = [
        path.name
        for path in capability.glob("*.py")
        if path.name != "__init__.py"
    ]
    assert stray == [], (
        f"能力插件必须放进功能域子目录（{' / '.join(sorted(CAPABILITY_GROUPS))}）：{stray}"
    )

    present = {path.name for path in capability.iterdir() if path.is_dir()}
    unknown = sorted(present - CAPABILITY_GROUPS - {"__pycache__"})
    assert unknown == [], f"capability/ 下出现了未登记的功能域：{unknown}"


def test_each_capability_group_has_a_docstring():
    """每个功能域要有包门面说明——否则新人不知道这一组是干什么的。"""
    capability = HERE / "plugins" / "capability"
    for name in sorted(CAPABILITY_GROUPS):
        init = capability / name / "__init__.py"
        assert init.is_file(), f"缺少 {name}/__init__.py"
        tree = ast.parse(init.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree)
        assert doc and len(doc.strip()) > 10, f"{name}/__init__.py 缺少有意义的说明"

