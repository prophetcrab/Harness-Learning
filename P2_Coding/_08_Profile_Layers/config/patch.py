"""Patch —— 按 id 定位行的"整块替换 / 追加"操作（配置分层的执行语义）。

M6 的配置分层靠两类操作落地（对齐 dsh 的 `cordis.patch.yml`）：

    replace  按 id 命中一行，**整块替换**（不是往 config 里做字段合并）
    insert   追加新行（id 必须全树唯一；追加在末尾——新行最后激活）

替换语义（dsh 的原话：a patch replaces the targeted row's whole config rather
than merging into it）在本实现里具体化为：

- patch 行给了 `name` → 换掉 name（**换 provider 就是这里，一行完成**）；
- patch 行给了 `config` → 整块换掉（`config: {}` 就是清空；不做逐字段 merge）；
- patch 行没给 `config` → 沿用目标行原来的 config；
- `disabled` 同理（给了就换，没给沿用）。

为什么"没给就沿用、给了就整块换"：换 provider（改 name）时通常不想再抄一遍
config；而一旦决定动 config，就要看见完整的最终值——字段级合并会让"这行最终
是什么"散在多个文件里，排查时得脑内求并集。宁可多写几行（dsh 说"each mode
bundle restates its complete configuration"），也不要隐式合并。

校验（全部 fail loud，铁律 #8）：
- replace 指向不存在的 id → 报错（拼错 id 不静默失效）；
- insert 撞已有 id / 撞本 patch 内其它 insert → 报错；
- patch 行没给任何可替换字段（name/config/disabled 全空）→ 报错（空操作是笔误）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from config.rows import ConfigError, ConfigRow, ConfigTree


@dataclass(frozen=True)
class PatchRow:
    """替换条目：id 必给；其余字段"给了就换、没给沿用"。"""

    id: str
    name: str | None = None
    config: dict[str, Any] | None = None
    disabled: bool | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ConfigError("patch 行缺少 id")
        if self.name is None and self.config is None and self.disabled is None:
            raise ConfigError(f"patch 行 {self.id} 什么都没改（name/config/disabled 全空）")

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "patch") -> PatchRow:
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ConfigError(f"{where}：patch 行必须是含 id 的字典")
        config = raw.get("config")
        if config is not None and not isinstance(config, dict):
            raise ConfigError(f"{where}：行 {raw['id']} 的 config 必须是字典")
        disabled = raw.get("disabled")
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]) if raw.get("name") is not None else None,
            config=dict(config) if config is not None else None,
            disabled=bool(disabled) if disabled is not None else None,
        )


@dataclass
class Patch:
    """一层补丁：若干替换 + 若干追加。"""

    replacements: list[PatchRow] = field(default_factory=list)
    inserts: list[ConfigRow] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "patch") -> Patch:
        """从 YAML 顶层形态解析：{"patch": [...], "insert": [...]}（两个键都可省）。"""
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}：补丁顶层必须是字典")
        unknown = set(raw) - {"patch", "insert"}
        if unknown:
            raise ConfigError(f"{where}：补丁里有未知键：{sorted(unknown)}（只认 patch / insert）")
        patches = raw.get("patch") or []
        inserts = raw.get("insert") or []
        if not isinstance(patches, list) or not isinstance(inserts, list):
            raise ConfigError(f"{where}：patch / insert 必须是列表")
        return cls(
            replacements=[PatchRow.from_dict(item, where=f"{where}.patch") for item in patches],
            inserts=[
                ConfigRow.from_dict(item, where=f"{where}.insert") for item in inserts
            ],
        )


def apply_patch(tree: ConfigTree, patch: Patch) -> ConfigTree:
    """把一层补丁应用到树上，返回**新树**（不改动输入）。

    替换保持行的原位置（骨架由 base 决定，patch 只换血肉）；insert 追加末尾。
    """
    result = tree.copy()

    # 替换：按 id 命中，原位整块换
    for entry in patch.replacements:
        target = result.get(entry.id)
        if target is None:
            known = "、".join(result.ids) or "（空树）"
            raise ConfigError(f"patch 指向不存在的行 id：{entry.id}（现有：{known}）")
        merged = ConfigRow(
            id=entry.id,
            name=entry.name if entry.name is not None else target.name,
            config=(
                copy.deepcopy(entry.config)
                if entry.config is not None
                else copy.deepcopy(target.config)
            ),
            disabled=entry.disabled if entry.disabled is not None else target.disabled,
        )
        result.rows[result.rows.index(target)] = merged

    # 追加：末尾（新行最后激活）；id 必须唯一
    for row in patch.inserts:
        if result.get(row.id) is not None:
            raise ConfigError(f"insert 的行 id 已存在：{row.id}")
        result.rows.append(ConfigRow(row.id, row.name, copy.deepcopy(row.config), row.disabled))

    return result


__all__ = ["Patch", "PatchRow", "apply_patch"]
