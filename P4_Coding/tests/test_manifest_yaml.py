"""YAML 装配单验收：launcher + 装配单 YAML → 一个最小应用。

钉住四件事（0054）：

    1. YAML 只是**数据**：能装出与 Python 装配单同样的插件树，插件工厂仍来自
       代码里的 catalog 白名单（YAML 不能凭空造插件）；
    2. 部署变量（工作区 / 会话目录 / 沙箱）由调用方填，改产品形态不改代码；
    3. 占位符 fail loud：未知变量、缺环境变量、未知条目字段都在启动前报错；
    4. YAML 装出来的世界能真的跑一个 turn（离线剧本，不联网）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import BootError, boot
from manifests.load import ManifestFileError, load_manifest
from manifests.minimal import catalog, manifest

HERE = Path(__file__).resolve().parents[1]
YAML = HERE / "manifests" / "minimal.yaml"

VARIABLES = {
    "workspace": "workspace",
    "sessions": "sessions",
    "sandbox": "changes",
    "approval_policy": "allow",
}


def ids(entries):
    """条目树 → {id: (name, role, parent_id)}（只用于比较形状）。"""
    found = {}

    def walk(items, parent=None):
        for item in items:
            found[item["id"]] = (item.get("name"), item.get("role"), parent)
            walk(item.get("children") or [], item["id"])

    walk(entries)
    return found


# ---------------------------------------------------------------------------
# 加载与形状
# ---------------------------------------------------------------------------


def test_yaml_manifest_loads():
    entries = load_manifest(YAML, variables=VARIABLES)
    assert len(entries) == 1
    assert entries[0]["id"] == "app"
    assert entries[0].get("role") is None  # 分组（没有 role 字段）
    assert ids(entries)["project"] == ("project", "base", "app")


def test_yaml_matches_the_python_manifest():
    """同一份产品形态：YAML 与 minimal.manifest() 的条目集合一致。

    唯一刻意不同的是模型后端：Python 版用 `llm-provider` + transport 剧本
    （YAML 表达不了函数），YAML 版用 `llm-fake` 加一个真实的 `deepseek` 路由。
    """
    loaded = load_manifest(YAML, variables=VARIABLES)
    python_entries = manifest(workspace="workspace", sessions="sessions")

    loaded_children = {item["id"]: item for item in loaded[0]["children"]}
    python_children = {item["id"]: item for item in python_entries[0]["children"]}

    assert set(loaded_children) - {"llm-fake", "llm-deepseek"} == set(python_children) - {
        "llm-provider"
    }
    for entry_id, item in python_children.items():
        if entry_id == "llm-provider":
            continue
        assert loaded_children[entry_id]["name"] == item["name"]
        assert loaded_children[entry_id]["role"] == item["role"]


def test_yaml_installs_both_model_routes():
    runtime = boot(load_manifest(YAML, variables=VARIABLES), catalog=catalog())
    try:
        from plugins.base.llm import LLM_SERVICE

        llm = runtime.ctx.require(LLM_SERVICE)
        assert llm.list_providers() == ("fake", "deepseek")
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 部署变量 / 占位符
# ---------------------------------------------------------------------------


def test_deploy_variables_are_filled_in(tmp_path):
    workspace = tmp_path / "ws"
    sessions = tmp_path / "ss"
    entries = load_manifest(
        YAML,
        variables={
            "workspace": str(workspace),
            "sessions": str(sessions),
            "sandbox": "free",
            "approval_policy": "deny",
        },
    )
    by_id = {item["id"]: item for item in entries[0]["children"]}
    assert by_id["workspace"]["config"]["root"] == str(workspace)
    assert by_id["store"]["config"]["root"] == str(sessions)
    assert by_id["sandbox"]["config"]["mode"] == "free"
    assert by_id["approval"]["config"]["policy"] == "deny"


def test_unknown_deploy_variable_fails_loud():
    with pytest.raises(ManifestFileError, match="未知部署变量"):
        load_manifest(YAML, variables={"workspace": "w"})


def test_env_placeholder_uses_default(monkeypatch):
    monkeypatch.delenv("P4_TEST_MODEL", raising=False)
    path = _write("""
- id: app
  children:
    - id: llm
      name: llm
      config:
        default_model: "${env:P4_TEST_MODEL:-fallback-model}"
""")
    try:
        entries = load_manifest(path)
        assert entries[0]["children"][0]["config"]["default_model"] == "fallback-model"
    finally:
        path.unlink()


def test_missing_env_variable_fails_loud(monkeypatch):
    monkeypatch.delenv("P4_TEST_REQUIRED", raising=False)
    path = _write("""
- id: app
  children:
    - id: llm
      name: llm
      config:
        api_key_env: "${env:P4_TEST_REQUIRED}"
""")
    try:
        with pytest.raises(ManifestFileError, match="P4_TEST_REQUIRED"):
            load_manifest(path)
    finally:
        path.unlink()


def test_whole_string_placeholder_keeps_its_type():
    path = _write("""
- id: app
  children:
    - id: x
      name: x
      config:
        count: "${count}"
        flag: true
""")
    try:
        entries = load_manifest(path, variables={"count": 10})
        assert entries[0]["children"][0]["config"]["count"] == 10
    finally:
        path.unlink()


# ---------------------------------------------------------------------------
# 形状错误：启动前就报，不带进运行时
# ---------------------------------------------------------------------------


def test_top_level_must_be_a_list():
    path = _write("app:\n  id: app\n")
    try:
        with pytest.raises(ManifestFileError, match="顶层必须是条目列表"):
            load_manifest(path)
    finally:
        path.unlink()


def test_unknown_entry_field_is_rejected():
    path = _write("""
- id: app
  children:
    - id: x
      name: x
      configs: {}
""")
    try:
        with pytest.raises(ManifestFileError, match="未知字段"):
            load_manifest(path)
    finally:
        path.unlink()


def test_non_list_children_is_rejected():
    path = _write("""
- id: app
  children: nope
""")
    try:
        with pytest.raises(ManifestFileError, match="children 必须是列表"):
            load_manifest(path)
    finally:
        path.unlink()


def test_unknown_plugin_name_fails_at_boot():
    """YAML 不能凭空造插件：名字必须在 catalog 白名单里。"""
    path = _write("""
- id: app
  children:
    - id: magic
      name: plugin-that-does-not-exist
      role: base
""")
    try:
        with pytest.raises(BootError, match="不在目录里"):
            boot(load_manifest(path), catalog=catalog())
    finally:
        path.unlink()


# ---------------------------------------------------------------------------
# 真 turn（离线）
# ---------------------------------------------------------------------------


def test_yaml_manifest_runs_a_turn(tmp_path):
    from plugins.base.project import PROJECT_SERVICE

    workspace = tmp_path / "ws"
    sessions = tmp_path / "ss"
    workspace.mkdir()
    entries = load_manifest(
        YAML,
        variables={
            "workspace": str(workspace),
            "sessions": str(sessions),
            "sandbox": "changes",
            "approval_policy": "allow",
        },
    )
    runtime = boot(entries, catalog=catalog())
    try:
        project = runtime.ctx.require(PROJECT_SERVICE)
        handle = project.start(session_id="yaml-app")
        try:
            result = handle.followup("你好")
        finally:
            handle.dispose()
        assert result.status == "done"
        assert result.final_text == "（离线模型）你好。"
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _write(text: str) -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    return Path(handle.name)
