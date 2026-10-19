"""分发包的公开面验收（0056）：`pip install p4-harness` 后 `import harness`。

钉住四件事：

    1. `import harness` 不会把插件包拖进来（分发单元 = core + loader）；
    2. `harness.boot` 就是 core 的启动器（门面不增加语义）；
    3. 只依赖公开面的**外部插件**能装进世界——插件协议是鸭子类型；
    4. YAML 装配单可以通过公开面读取（数据随包分发）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import core
import harness

HERE = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. 门面是纯 re-export，且不拖入插件
# ---------------------------------------------------------------------------


def test_facade_is_core_boot():
    assert harness.boot is core.boot
    assert harness.Context is core.Context
    assert harness.Runtime is core.Runtime


def test_import_harness_does_not_load_plugins():
    """分发包的最小单元是 core + loader；插件由使用方的 catalog 提供。"""
    code = (
        "import sys; import harness; "
        "assert 'plugins' not in sys.modules, "
        "sorted(k for k in sys.modules if k.startswith('plugins')); "
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
# 2. 空装配单也能起（基座自足）
# ---------------------------------------------------------------------------


def test_boot_with_empty_manifest_and_catalog():
    runtime = harness.boot([], catalog={})
    try:
        assert runtime.ctx.services == ["loader"]
        assert runtime.ctx.active_plugins == ["loader"]
        assert runtime.snapshot() == ()
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 3. 外部插件：不 import 项目的任何插件，只用公开面
# ---------------------------------------------------------------------------


class Greeter:
    """一个"第三方"插件：普通类，鸭子类型满足插件协议。

    - `inject` 声明必需服务；
    - `setup(ctx)` 里用 ctx.provide / ctx.on；
    - setup 返回的清理函数随 effect 回卷。
    """

    name = "greeter"
    inject = ("config",)

    def setup(self, ctx):
        def greet() -> str:
            return f"hello, {ctx.require('config')}"

        ctx.provide("greeting", greet)
        ctx.on("hello", lambda **_: None)

        def dispose() -> None:
            pass

        return dispose


def test_external_plugin_uses_only_the_public_surface():
    catalog = {
        "config": lambda who="world": _ConfigPlugin(who),
        "greeter": lambda **_: Greeter(),
    }
    entries = [
        {"id": "app", "children": [
            {"id": "config", "name": "config", "config": {"who": "harness"}},
            {"id": "greeter", "name": "greeter"},
        ]},
    ]
    runtime = harness.boot(entries, catalog=catalog)
    try:
        assert runtime.ctx.require("greeting")() == "hello, harness"
        assert "greeter" in runtime.ctx.active_plugins
        assert runtime.ctx.event_names == ["hello"]
    finally:
        runtime.close()


class _ConfigPlugin:
    name = "config"

    def __init__(self, who: str) -> None:
        self._who = who

    def setup(self, ctx):
        ctx.provide("config", self._who)


# ---------------------------------------------------------------------------
# 4. YAML 装配单也走公开面
# ---------------------------------------------------------------------------


def test_public_facade_loads_yaml_manifest(tmp_path):
    path = tmp_path / "app.yaml"
    path.write_text(
        """
- id: app
  children:
    - id: config
      name: config
      config:
        who: "${who}"
""",
        encoding="utf-8",
    )
    entries = harness.load_manifest(path, variables={"who": "yaml"})
    runtime = harness.boot(
        entries,
        catalog={"config": lambda who="world": _ConfigPlugin(who)},
    )
    try:
        assert runtime.ctx.require("config") == "yaml"
    finally:
        runtime.close()


def test_manifest_errors_are_public():
    assert issubclass(harness.ManifestFileError, RuntimeError)
