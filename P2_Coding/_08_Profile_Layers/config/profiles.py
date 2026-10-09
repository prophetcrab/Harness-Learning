"""Profile 分层加载 —— base → profile patch → 用户 patch → CLI patch。

M6 的配置组装顺序（对齐 dsh `profile-boot.ts`："stack its patch layers ...
the profile's own cordis.patch.yml, --patch overlays"）：

    1. base.yaml            底座树（能力清单 + 中性默认值）
    2. <name>.yaml          profile 补丁（如 dev：换成假实现组合）
    3. user.patch.yaml      用户补丁（可选；个人覆盖，不入库的惯例）
    4. CLI --patch 文件      命令行补丁（可多次；最后一次在最后）

**后层覆盖前层，按行（row）为单位**——不是整树替换。每层都可以只动它关心的行，
其余行从下层原样流过来。最终树的每一行，其值来自"**最后一个碰过它的层**"。
这让"同 base、两个 profile 产出不同且可读的树"成为自然结果：差异全部落在
各层实际写过的那些行上。

层与层之间只认数据（ConfigTree / Patch），不碰 kernel 与 providers——
配置是纯数据层，激活（boot）是装配层的事（见 providers/plugins.py 的 boot_tree）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from config.patch import Patch, apply_patch
from config.rows import ConfigError, ConfigTree

BASE_FILENAME = "base.yaml"
USER_PATCH_FILENAME = "user.patch.yaml"


def _read_yaml(path: Path, *, what: str) -> dict:
    """读一个 YAML 文件为字典；不存在/解析失败 fail loud 并指出路径。"""
    if not path.is_file():
        raise ConfigError(f"{what}不存在：{path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{what}不是合法 YAML：{path}（{exc}）") from exc
    if raw is None:
        raise ConfigError(f"{what}是空文件：{path}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{what}顶层必须是字典：{path}")
    return raw


def load_tree(path: str | Path) -> ConfigTree:
    """从 YAML 文件加载一棵配置树（{"rows": [...]} 形态）。"""
    path = Path(path)
    return ConfigTree.from_mapping(_read_yaml(path, what="配置树文件"), where=str(path))


def load_patch(path: str | Path) -> Patch:
    """从 YAML 文件加载一层补丁（{"patch": [...], "insert": [...]} 形态）。"""
    path = Path(path)
    return Patch.from_dict(_read_yaml(path, what="补丁文件"), where=str(path))


def compose(tree: ConfigTree, layers: list[Patch]) -> ConfigTree:
    """按顺序把多层补丁叠到树上（后层覆盖前层）。"""
    result = tree
    for patch in layers:
        result = apply_patch(result, patch)
    return result


def load_profile(
    name: str,
    *,
    profiles_dir: str | Path,
    cli_patches: list[str | Path] = (),
) -> ConfigTree:
    """按名字加载一个 profile 的最终配置树。

    层序：base.yaml → <name>.yaml → user.patch.yaml（存在才用）→ cli_patches（按序）。
    """
    profiles_dir = Path(profiles_dir)
    tree = load_tree(profiles_dir / BASE_FILENAME)

    layers: list[Patch] = [load_patch(profiles_dir / f"{name}.yaml")]
    user_patch = profiles_dir / USER_PATCH_FILENAME
    if user_patch.is_file():
        layers.append(load_patch(user_patch))
    layers.extend(load_patch(path) for path in cli_patches)

    return compose(tree, layers)


__all__ = [
    "BASE_FILENAME",
    "USER_PATCH_FILENAME",
    "load_tree",
    "load_patch",
    "compose",
    "load_profile",
]
