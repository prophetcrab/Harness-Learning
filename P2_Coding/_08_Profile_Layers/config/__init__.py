"""config —— M6 的配置层（阶段 `_08` 新增的顶层模块）：能力组合从代码变成数据。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级；`harness/` 冻结为 M1–M3
基线库，本阶段一行未改。本包机制一句话：

    装配不再写代码，而是**数据**——一棵有序的行（row）组成的配置树；
    profiles 是叠在底座上的补丁层（base → profile → user → CLI），
    后层按行覆盖前层；换 provider 只改一行的 name。

组件：

- `ConfigRow` / `ConfigTree`（rows.py）   行与树：id/name/config/disabled，有序、id 唯一；
- `Patch` / `apply_patch`（patch.py）     按 id 整块替换 + 追加；校验全部 fail loud；
- `load_tree` / `load_patch` / `load_profile` / `compose`（profiles.py）
                                          分层加载（YAML）与叠加；
- 激活（把树 boot 成可运行的 ctx）在 **providers/plugins.py 的 `boot_tree`**——
  配置层保持纯数据（不依赖 kernel/providers），装配层负责"数据 → 插件 → ctx"。

用法（阶段目录下）：

    tree = load_profile("dev", profiles_dir="profiles")
    ctx = boot_tree(tree, stage_root=Path("."))     # providers/plugins.py
    llm = ctx.require(LLM_CAPABILITY)
    toolbox = ctx.require(TOOLBOX_CAPABILITY)

依赖方向：`config → yaml + 自身`（纯数据层）；`providers → config`（boot_tree 消费树）。
"""

from config.patch import Patch, PatchRow, apply_patch
from config.profiles import (
    BASE_FILENAME,
    USER_PATCH_FILENAME,
    compose,
    load_patch,
    load_profile,
    load_tree,
)
from config.rows import ConfigError, ConfigRow, ConfigTree

__all__ = [
    # 行与树
    "ConfigError",
    "ConfigRow",
    "ConfigTree",
    # 补丁
    "Patch",
    "PatchRow",
    "apply_patch",
    # 分层加载
    "BASE_FILENAME",
    "USER_PATCH_FILENAME",
    "load_tree",
    "load_patch",
    "load_profile",
    "compose",
]
