"""配置树 —— 能力组合的**数据形态**（行 + 树）。

M6 的第二个机制（`_08` 的核心词汇）。`_07` 里装配是代码（`load_plugins(ctx, [make_fs_plugin(...)])`）；
本阶段把"装什么"抽成**数据**：一棵有序的行（row）组成的树。

每一行描述一个插件：

    id      行的身份（patch 按它定位；全树唯一）
    name    插件名（对应 providers 里的工厂，如 "fs:memory"、"llm:fake"）
    config  该插件的参数（字典；整块替换语义见 patch.py）
    disabled 保留在树里但不激活（dsh 风格：想临时关掉某行时不删行）

为什么行要有 id 而不是只有 name：同一类插件可以有多行（如两个 fs 行做对照实验），
patch 要能精确指向"那一行"；id 是稳定锚点，name 是实现选择。**换 provider 只改一行的
name**——这就是本阶段验收里那句"换 LLM provider 只改一行"的落点。

树是有序的：**行序 = 激活顺序**（dsh 说"row order carries no load semantics"，
因为它的激活由服务可用性驱动；我们保持朴素——顺序激活，依赖行在前——
`toolbox` 行依赖 `fs` 行，顺序错了会在装载时 fail loud）。

对应 dsh：`cordis.patch.yml` 的行（`id` / `name` / `config` / `disabled`）
与 `apps/cli/src/profile-boot.ts` 的"在空根上叠加补丁层"。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class ConfigError(ValueError):
    """配置层的 fail loud：字段缺失/重复/非法值，启动时立刻报错（铁律 #8）。"""


@dataclass(frozen=True)
class ConfigRow:
    """配置树里的一行：一个插件及其参数。"""

    id: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ConfigError("配置行缺少 id")
        if not self.name:
            raise ConfigError(f"配置行 {self.id} 缺少 name")

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, where: str = "row") -> ConfigRow:
        """从 YAML 字典构造一行；缺字段 fail loud 并指出位置。"""
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}：配置行必须是字典，收到 {type(raw).__name__}")
        row_id = raw.get("id", "")
        name = raw.get("name", "")
        if not row_id or not name:
            raise ConfigError(f"{where}：配置行缺少 id 或 name（收到 {raw!r}）")
        config = raw.get("config") or {}
        if not isinstance(config, dict):
            raise ConfigError(f"{where}：行 {row_id} 的 config 必须是字典")
        return cls(
            id=str(row_id),
            name=str(name),
            config=dict(config),
            disabled=bool(raw.get("disabled", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        """转回可序列化的字典（dump-config 与快照测试用）。"""
        data: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.config:
            data["config"] = dict(self.config)
        if self.disabled:
            data["disabled"] = True
        return data


@dataclass
class ConfigTree:
    """一棵有序的配置树：行按激活顺序排列，id 全树唯一。"""

    rows: list[ConfigRow] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, row_id: str) -> ConfigRow | None:
        """按 id 取行；不存在返回 None。"""
        for row in self.rows:
            if row.id == row_id:
                return row
        return None

    def require(self, row_id: str) -> ConfigRow:
        """按 id 取行；不存在 → ConfigError（fail loud）。"""
        row = self.get(row_id)
        if row is None:
            known = "、".join(r.id for r in self.rows) or "（空树）"
            raise ConfigError(f"配置树里没有行 {row_id!r}（现有：{known}）")
        return row

    @property
    def ids(self) -> list[str]:
        """全部行 id，按顺序。"""
        return [row.id for row in self.rows]

    def active_rows(self) -> list[ConfigRow]:
        """激活的行（跳过 disabled）——boot 用的就是这一份。"""
        return [row for row in self.rows if not row.disabled]

    # ------------------------------------------------------------------
    # 构造与序列化
    # ------------------------------------------------------------------

    @classmethod
    def from_dicts(cls, raws: list[dict[str, Any]], *, where: str = "tree") -> ConfigTree:
        """从一组字典构造树；id 重复 fail loud（行身份必须唯一）。"""
        rows: list[ConfigRow] = []
        seen: set[str] = set()
        for index, raw in enumerate(raws):
            row = ConfigRow.from_dict(raw, where=f"{where}[{index}]")
            if row.id in seen:
                raise ConfigError(f"{where}：行 id 重复：{row.id}")
            seen.add(row.id)
            rows.append(row)
        return cls(rows=rows)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], *, where: str = "tree") -> ConfigTree:
        """从 {"rows": [...]} 形态的字典构造树（YAML 文件的顶层形态）。"""
        if not isinstance(raw, dict) or "rows" not in raw:
            raise ConfigError(f"{where}：顶层必须是 {{'rows': [...]}} 形态")
        rows = raw["rows"]
        if not isinstance(rows, list):
            raise ConfigError(f"{where}：rows 必须是列表")
        return cls.from_dicts(rows, where=where)

    def to_dict(self) -> dict[str, Any]:
        """转回可序列化字典（快照/dump 用）。"""
        return {"rows": [row.to_dict() for row in self.rows]}

    def copy(self) -> ConfigTree:
        """深拷贝一份（分层叠加时不改动下层；config 里的嵌套也隔离）。"""
        return ConfigTree(
            rows=[
                ConfigRow(r.id, r.name, copy.deepcopy(r.config), r.disabled) for r in self.rows
            ]
        )


__all__ = ["ConfigError", "ConfigRow", "ConfigTree"]
