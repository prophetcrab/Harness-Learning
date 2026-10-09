"""_08_Profile_Layers 的单元测试：配置树 / patch 语义 / 分层加载。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（config/ 包：rows + patch + profiles），以及 providers 侧的树→插件工厂。

覆盖：
1. 行与树：必备字段、id 唯一、读取/缺省、active_rows、序列化往返
2. patch：整块替换（config 不合并）、保持原位、insert 追加、字段级"给了才换"
3. fail loud：未知 id / 重复 insert / 空 patch 行 / 坏 YAML / 非法结构
4. 分层：base → profile → user → CLI 的顺序（后写胜出）；同 base 两 profile 对照
5. 工厂：未知 name / 合法 name → 插件对象（构造阶段无副作用）

运行：cd P2_Coding/_08_Profile_Layers && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import (
    ConfigError,
    ConfigRow,
    ConfigTree,
    Patch,
    PatchRow,
    apply_patch,
    compose,
    load_patch,
    load_profile,
    load_tree,
)
from providers import PLUGIN_FACTORIES, build_plugins_from_tree

PROFILES = Path(__file__).resolve().parents[1] / "profiles"


# =========================================================================
# 1) 行与树
# =========================================================================


def test_row_requires_id_and_name():
    with pytest.raises(ConfigError, match="缺少 id"):
        ConfigRow(id="", name="fs:local")
    with pytest.raises(ConfigError, match="缺少 name"):
        ConfigRow(id="fs", name="")


def test_tree_get_and_require():
    tree = ConfigTree.from_dicts(
        [{"id": "llm", "name": "llm:fake"}, {"id": "fs", "name": "fs:memory", "config": {"x": 1}}]
    )
    assert tree.get("llm").name == "llm:fake"
    assert tree.get("nope") is None
    with pytest.raises(ConfigError, match="没有行"):
        tree.require("nope")
    assert tree.require("fs").config == {"x": 1}


def test_tree_rejects_duplicate_ids():
    with pytest.raises(ConfigError, match="id 重复"):
        ConfigTree.from_dicts([{"id": "a", "name": "x"}, {"id": "a", "name": "y"}])


def test_active_rows_skip_disabled():
    tree = ConfigTree.from_dicts(
        [
            {"id": "a", "name": "x"},
            {"id": "b", "name": "y", "disabled": True},
            {"id": "c", "name": "z"},
        ]
    )
    assert tree.ids == ["a", "b", "c"]
    assert [row.id for row in tree.active_rows()] == ["a", "c"]


def test_serialization_round_trip():
    tree = ConfigTree.from_dicts(
        [{"id": "llm", "name": "llm:fake"}, {"id": "fs", "name": "fs:jail", "config": {"root": "w"}}]
    )
    restored = ConfigTree.from_mapping(tree.to_dict())
    assert restored.to_dict() == tree.to_dict()


def test_copy_is_deep():
    tree = ConfigTree.from_dicts([{"id": "a", "name": "x", "config": {"deep": {"v": 1}}}])
    clone = tree.copy()
    clone.rows[0].config["deep"]["v"] = 2
    assert tree.rows[0].config["deep"]["v"] == 1  # 原树不受影响


# =========================================================================
# 2) patch 语义
# =========================================================================


def _base_tree() -> ConfigTree:
    return ConfigTree.from_dicts(
        [
            {"id": "llm", "name": "llm:fake", "config": {"model": "m1", "extra": True}},
            {"id": "fs", "name": "fs:local", "config": {"root": "ws"}},
            {"id": "toolbox", "name": "toolbox", "config": {"shell_timeout": 15}},
        ]
    )


def test_replace_swaps_whole_config_not_merge():
    """整块替换语义：给了 config 就整个换掉，不做字段合并。"""
    tree = apply_patch(
        _base_tree(),
        Patch(replacements=[PatchRow(id="llm", config={"model": "m2"})]),
    )
    assert tree.require("llm").config == {"model": "m2"}  # extra 没了（不是合并）


def test_replace_without_config_keeps_it():
    """没给 config 就沿用原值——换 name 时不必抄一遍 config。"""
    tree = apply_patch(
        _base_tree(),
        Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")]),
    )
    assert tree.require("llm").name == "llm:deepseek"
    assert tree.require("llm").config == {"model": "m1", "extra": True}


def test_replace_keeps_position():
    """替换保持行的原位置（骨架由 base 决定）。"""
    tree = apply_patch(
        _base_tree(),
        Patch(replacements=[PatchRow(id="fs", name="fs:memory", config={})]),
    )
    assert tree.ids == ["llm", "fs", "toolbox"]


def test_insert_appends_to_end():
    tree = apply_patch(
        _base_tree(),
        Patch(inserts=[ConfigRow(id="extra", name="fs:memory")]),
    )
    assert tree.ids == ["llm", "fs", "toolbox", "extra"]


def test_disable_and_reenable():
    disabled = apply_patch(_base_tree(), Patch(replacements=[PatchRow(id="llm", disabled=True)]))
    assert disabled.require("llm").disabled is True
    assert disabled.require("llm").name == "llm:fake"  # 其余字段不动
    reenabled = apply_patch(disabled, Patch(replacements=[PatchRow(id="llm", disabled=False)]))
    assert reenabled.require("llm").disabled is False


def test_apply_patch_does_not_mutate_input():
    tree = _base_tree()
    apply_patch(tree, Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")]))
    assert tree.require("llm").name == "llm:fake"  # 原树不变


# =========================================================================
# 3) fail loud
# =========================================================================


def test_replace_unknown_id_is_loud():
    with pytest.raises(ConfigError, match="不存在"):
        apply_patch(_base_tree(), Patch(replacements=[PatchRow(id="ghost", disabled=True)]))


def test_insert_duplicate_id_is_loud():
    with pytest.raises(ConfigError, match="已存在"):
        apply_patch(_base_tree(), Patch(inserts=[ConfigRow(id="llm", name="x")]))


def test_empty_patch_row_is_loud():
    with pytest.raises(ConfigError, match="什么都没改"):
        PatchRow(id="llm")


def test_unknown_patch_key_is_loud():
    with pytest.raises(ConfigError, match="未知键"):
        Patch.from_dict({"patch": [], "bogus": []})


def test_bad_yaml_is_loud(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("patch: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法 YAML"):
        load_patch(bad)


def test_missing_file_is_loud(tmp_path: Path):
    with pytest.raises(ConfigError, match="不存在"):
        load_tree(tmp_path / "nope.yaml")


def test_tree_top_level_shape_is_loud(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("rows: {not: a-list}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="必须是列表"):
        load_tree(bad)


# =========================================================================
# 4) 分层（用真实 profiles/ 目录）
# =========================================================================


def test_dev_and_prod_share_skeleton_but_differ_in_three_rows():
    dev = load_profile("dev", profiles_dir=PROFILES)
    prod = load_profile("prod", profiles_dir=PROFILES)

    assert dev.ids == prod.ids == ["llm", "fs", "subprocess", "toolbox"]
    changed = [
        row.id
        for row, other in zip(dev.rows, prod.rows, strict=True)
        if (row.name, row.config) != (other.name, other.config)
    ]
    assert changed == ["llm", "fs", "subprocess"]
    assert dev.require("llm").name == "llm:fake"
    assert prod.require("llm").name == "llm:deepseek"
    assert prod.require("fs").name == "fs:local"


def test_cli_patch_wins_over_profile(tmp_path: Path):
    cli = tmp_path / "cli.yaml"
    cli.write_text(
        "patch:\n  - id: toolbox\n    config:\n      shell_timeout: 3\n", encoding="utf-8"
    )
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    assert tree.require("toolbox").config["shell_timeout"] == 3


def test_cli_patches_apply_in_order(tmp_path: Path):
    first = tmp_path / "a.yaml"
    first.write_text("patch:\n  - id: toolbox\n    config:\n      shell_timeout: 9\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("patch:\n  - id: toolbox\n    config:\n      shell_timeout: 1\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[first, second])
    assert tree.require("toolbox").config["shell_timeout"] == 1  # 后写胜出


def test_user_patch_layer_applies_between_profile_and_cli(tmp_path: Path):
    """层序验证：直接 compose 三层——user 覆盖 profile、CLI 覆盖 user。"""
    base = ConfigTree.from_dicts([{"id": "x", "name": "n0", "config": {"v": 0}}])
    profile = Patch(replacements=[PatchRow(id="x", name="n1", config={"v": 1})])
    user = Patch(replacements=[PatchRow(id="x", name="n2", config={"v": 2})])
    cli = Patch(replacements=[PatchRow(id="x", name="n3", config={"v": 3})])
    final = compose(base, [profile, user, cli])
    assert final.require("x").name == "n3" and final.require("x").config["v"] == 3


def test_unknown_profile_is_loud():
    with pytest.raises(ConfigError, match="不存在"):
        load_profile("ghost", profiles_dir=PROFILES)


def test_base_alone_is_usable():
    """base 层自己就是一棵合法树（中性默认；不含任何环境特定选择）。"""
    tree = load_tree(PROFILES / "base.yaml")
    assert tree.ids == ["llm", "fs", "subprocess", "toolbox"]
    assert tree.require("llm").name == "llm:fake"  # 中性默认不联网


# =========================================================================
# 5) 工厂：树 → 插件对象（构造阶段）
# =========================================================================


def test_factories_cover_every_name_used_by_profiles():
    for name in ("dev", "prod"):
        tree = load_profile(name, profiles_dir=PROFILES)
        for row in tree.active_rows():
            assert row.name in PLUGIN_FACTORIES, f"{name} 用了未注册的 name：{row.name}"


def test_build_plugins_from_tree_constructs_without_side_effects():
    tree = load_profile("dev", profiles_dir=PROFILES)
    plugins = build_plugins_from_tree(tree, stage_root=PROFILES.parent)
    assert [p.name for p in plugins] == [
        "llm:FakeLLM",
        "fs:MemoryFS",
        "subprocess:ScriptedSubprocess",
        "toolbox",
    ]


def test_build_plugins_skips_disabled_rows():
    tree = apply_patch(
        load_profile("dev", profiles_dir=PROFILES),
        Patch(replacements=[PatchRow(id="subprocess", disabled=True)]),
    )
    plugins = build_plugins_from_tree(tree, stage_root=PROFILES.parent)
    assert [p.name for p in plugins] == ["llm:FakeLLM", "fs:MemoryFS", "toolbox"]


def test_unknown_plugin_name_is_loud_before_boot():
    tree = apply_patch(
        load_profile("dev", profiles_dir=PROFILES),
        Patch(replacements=[PatchRow(id="llm", name="llm:ghost")]),
    )
    with pytest.raises(ConfigError, match="name 未知"):
        build_plugins_from_tree(tree, stage_root=PROFILES.parent)
