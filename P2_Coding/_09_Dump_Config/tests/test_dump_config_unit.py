"""_09_Dump_Config 的单元测试：来源追踪、dump 渲染、错误定位。

与同目录另两个基线文件（22 用例）的分工：本文件只测试**本阶段新增的机制**
（config 的 sources 台账 + dump.py 渲染 + 四类配置错误的定位），
以及 `_08` 关键语义的回归（patch 整块替换/保持原位/后写胜出——`_09` 改的正是
这两处，语义必须被重新钉住）。

覆盖：
1. 来源追踪：初始台账 = base 文件名；patch 只覆盖它实际改过的字段；
   CLI 多层带序号；insert 的行来源 = 插入层；纯数据构造无台账
2. 渲染：render_tree 含每项来源、行序、禁用标记；source_summary 按层汇总
3. 错误定位：层名 + 条目序号（replace 未知 id / insert 撞 id）；
   拼写建议（id 与插件名）；未知 config 键；参数类型错的工厂包装；YAML 语法错带路径
4. `_08` 语义回归：整块替换不合并、保持原位、后写胜出、未知键 fail loud

运行：cd P2_Coding/_09_Dump_Config && python -m pytest -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import (
    UNKNOWN_SOURCE,
    ConfigError,
    ConfigRow,
    ConfigTree,
    Patch,
    PatchRow,
    apply_patch,
    load_patch,
    load_profile,
    render_tree,
    source_summary,
)
from providers import PLUGIN_CONFIG_KEYS, build_plugins_from_tree

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
STAGE_ROOT = Path(__file__).resolve().parents[1]


# =========================================================================
# 1) 来源追踪
# =========================================================================


def test_initial_tree_records_base_source():
    tree = load_profile("dev", profiles_dir=PROFILES)
    assert tree.source_of("toolbox", "name") == "base.yaml"  # dev 没碰 toolbox
    assert tree.source_of("llm", "name") == "dev.yaml"       # dev 把 llm 换掉了


def test_patch_records_only_fields_it_touches():
    """patch 只覆盖它实际改过的字段；沿用的字段保持旧来源（字段级粒度）。"""
    tree = load_profile("dev", profiles_dir=PROFILES)
    # 只换 name、不动 config → config 的来源应保持 dev.yaml
    switched = apply_patch(
        tree, Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")], label="cli.yaml")
    )
    assert switched.source_of("llm", "name") == "cli.yaml"
    assert switched.source_of("llm", "config") == "dev.yaml"  # 没被碰，来源不变


def test_cli_patches_get_numbered_labels(tmp_path: Path):
    first = tmp_path / "a.yaml"
    first.write_text("patch:\n  - id: llm\n    name: llm:fake\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("patch:\n  - id: fs\n    name: fs:memory\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[first, second])
    assert tree.source_of("llm", "name") == "CLI#1 a.yaml"
    assert tree.source_of("fs", "name") == "CLI#2 b.yaml"


def test_inserted_row_source_is_the_inserting_layer(tmp_path: Path):
    extra = tmp_path / "extra.yaml"
    extra.write_text("insert:\n  - id: fs-extra\n    name: fs:memory\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[extra])
    assert tree.source_of("fs-extra", "name") == "CLI#1 extra.yaml"
    assert tree.require("fs-extra").name == "fs:memory"


def test_earlier_uncertainty_shown_when_no_ledger():
    """纯数据构造（不走加载器）没有台账——渲染时给占位而不是乱猜。"""
    tree = ConfigTree.from_dicts([{"id": "a", "name": "x"}])
    assert tree.source_of("a", "name") is None
    assert UNKNOWN_SOURCE in render_tree(tree)


def test_copy_carries_sources():
    tree = load_profile("dev", profiles_dir=PROFILES)
    clone = tree.copy()
    assert clone.source_of("llm", "name") == "dev.yaml"
    apply_patch(clone, Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")], label="x"))
    assert tree.source_of("llm", "name") == "dev.yaml"  # 原树来源不变


# =========================================================================
# 2) dump 渲染
# =========================================================================


def test_render_tree_lists_every_field_with_source():
    tree = load_profile("dev", profiles_dir=PROFILES)
    text = render_tree(tree, title="profile = dev")

    assert "profile = dev" in text
    assert "共 4 行" in text
    for row_id in ("llm", "fs", "subprocess", "toolbox"):
        assert row_id in text
    # 每项带来源箭头
    assert "← dev.yaml" in text and "← base.yaml" in text
    # 行序 = 激活顺序
    assert text.index("1. llm") < text.index("2. fs") < text.index("4. toolbox")


def test_render_tree_shows_disabled_mark(tmp_path: Path):
    cli = tmp_path / "cli.yaml"
    cli.write_text("patch:\n  - id: subprocess\n    disabled: true\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    text = render_tree(tree)
    assert "（已禁用）" in text and "disabled" in text
    assert "激活 3 行" in text


def test_render_tree_shows_config_values():
    tree = load_profile("dev", profiles_dir=PROFILES)
    text = render_tree(tree)
    assert "config.workspace" in text and "demo_workspace/ws" in text
    assert "config.shell_timeout" in text and "15" in text


def test_source_summary_groups_by_layer(tmp_path: Path):
    cli = tmp_path / "cli.yaml"
    cli.write_text("patch:\n  - id: llm\n    name: llm:deepseek\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[cli])
    summary = source_summary(tree)
    assert "llm.name" in summary["CLI#1 cli.yaml"]
    assert "fs.name" in summary["dev.yaml"]
    assert "toolbox.name" in summary["base.yaml"]


# =========================================================================
# 3) 错误定位
# =========================================================================


def _tree():
    return load_profile("dev", profiles_dir=PROFILES)


def test_unknown_row_id_error_names_layer_and_position():
    with pytest.raises(ConfigError) as info:
        apply_patch(
            _tree(),
            Patch(replacements=[PatchRow(id="nope", disabled=True)], label="dev.yaml"),
        )
    message = str(info.value)
    assert "dev.yaml 第 1 条 patch" in message
    assert "'nope' 不存在" in message
    assert "现有：llm、fs、subprocess、toolbox" in message


def test_unknown_row_id_gets_spelling_suggestion():
    with pytest.raises(ConfigError, match="你是不是想改 'toolbox'"):
        apply_patch(
            _tree(), Patch(replacements=[PatchRow(id="toolbx", disabled=True)], label="x.yaml")
        )


def test_insert_duplicate_error_names_layer_and_position():
    with pytest.raises(ConfigError) as info:
        apply_patch(
            _tree(),
            Patch(inserts=[ConfigRow(id="llm", name="llm:fake")], label="my.yaml"),
        )
    message = str(info.value)
    assert "my.yaml 第 1 条 insert" in message and "已存在" in message


def test_unknown_plugin_name_error_names_row_and_suggests():
    tree = apply_patch(
        _tree(), Patch(replacements=[PatchRow(id="llm", name="llm:fake2")], label="x")
    )
    with pytest.raises(ConfigError) as info:
        build_plugins_from_tree(tree, stage_root=STAGE_ROOT)
    message = str(info.value)
    assert "行 'llm'" in message and "llm:fake2" in message
    assert "你是不是想写 'llm:fake'" in message


def test_unknown_config_key_error_lists_allowed_and_suggests():
    tree = apply_patch(
        _tree(),
        Patch(
            replacements=[PatchRow(id="toolbox", config={"shell_timeou": 3})],
            label="x.yaml",
        ),
    )
    with pytest.raises(ConfigError) as info:
        build_plugins_from_tree(tree, stage_root=STAGE_ROOT)
    message = str(info.value)
    assert "toolbox" in message and "shell_timeou" in message
    assert "你是不是想写 'shell_timeout'" in message


def test_bad_param_type_error_names_row():
    tree = apply_patch(
        _tree(),
        Patch(replacements=[PatchRow(id="toolbox", config={"shell_timeout": "很快"})], label="x"),
    )
    with pytest.raises(ConfigError, match="行 'toolbox'.*参数有问题"):
        build_plugins_from_tree(tree, stage_root=STAGE_ROOT)


def test_yaml_syntax_error_names_file(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("patch: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_patch(bad)
    assert "不是合法 YAML" in str(info.value) and "bad.yaml" in str(info.value)


def test_row_unknown_key_is_loud():
    with pytest.raises(ConfigError, match="未知键"):
        ConfigRow.from_dict({"id": "a", "name": "x", "confg": {}}, where="t")


def test_patch_row_unknown_key_is_loud():
    with pytest.raises(ConfigError, match="未知键"):
        Patch.from_dict({"patch": [{"id": "llm", "confg": {}}]})


def test_all_factories_declare_config_keys():
    """每个工厂都在白名单里有记录（防止新增插件忘登记 → 参数校验名存实亡）。"""
    from providers import PLUGIN_FACTORIES

    assert set(PLUGIN_CONFIG_KEYS) == set(PLUGIN_FACTORIES)


# =========================================================================
# 4) `_08` 语义回归（本阶段改动了 config 包，这些必须重新钉住）
# =========================================================================


def test_replace_swaps_whole_config_not_merge():
    tree = apply_patch(
        ConfigTree.from_dicts(
            [{"id": "llm", "name": "a", "config": {"model": "m1", "extra": True}}]
        ),
        Patch(replacements=[PatchRow(id="llm", config={"model": "m2"})]),
    )
    assert tree.require("llm").config == {"model": "m2"}


def test_apply_patch_does_not_mutate_input():
    tree = _tree()
    apply_patch(tree, Patch(replacements=[PatchRow(id="llm", name="llm:deepseek")]))
    assert tree.require("llm").name == "llm:fake"


def test_later_layer_wins(tmp_path: Path):
    first = tmp_path / "a.yaml"
    first.write_text("patch:\n  - id: llm\n    name: llm:deepseek\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("patch:\n  - id: llm\n    name: llm:fake\n", encoding="utf-8")
    tree = load_profile("dev", profiles_dir=PROFILES, cli_patches=[first, second])
    assert tree.require("llm").name == "llm:fake"
    assert tree.source_of("llm", "name") == "CLI#2 b.yaml"


def test_empty_patch_row_still_loud():
    with pytest.raises(ConfigError, match="什么都没改"):
        PatchRow(id="llm")
