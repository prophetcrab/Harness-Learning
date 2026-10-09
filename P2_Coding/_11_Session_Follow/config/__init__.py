"""config —— M6 的配置层（阶段 `_09` 的顶层模块）：能力组合从代码变成数据。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级；`harness/` 冻结为 M1–M3
基线库，本阶段一行未改。本包机制（累计推进）：

- `_08`：装配是**数据**——一棵有序的行（row）组成的配置树；profiles 是叠在底座上的
  补丁层（base → profile → user → CLI），后层按行覆盖前层；换 provider 只改一行。
- `_09`：配置**可见、可定位**——每个字段带**来源台账**（`← dev.yaml` / `← CLI#1 x.yaml`）；
  `render_tree` 把它渲染成人能读的清单；配置错误带层标签、条目序号与拼写建议。

组件：

- `ConfigRow` / `ConfigTree`（rows.py）   行与树：id/name/config/disabled；树带来源台账；
- `Patch` / `PatchRow` / `apply_patch`（patch.py）
                                          按 id 整块替换 + 追加；错误带层名与条目号；
- `load_tree` / `load_patch` / `load_profile` / `compose`（profiles.py）
                                          分层加载（YAML）；加载即填来源台账；
- `render_tree` / `source_summary`（dump.py）
                                          可读渲染（每项来源）与按层汇总；
- 激活（把树 boot 成可运行的 ctx）在 **providers/plugins.py 的 `boot_tree`**。

用法（阶段目录下）：

    tree = load_profile("dev", profiles_dir="profiles")
    print(render_tree(tree, title="profile = dev"))
    ctx = boot_tree(tree, stage_root=Path("."))

依赖方向：`config → yaml + 自身`（纯数据层）；`providers → config`（boot_tree 消费树）。
"""

from config.dump import UNKNOWN_SOURCE, render_tree, source_summary
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
    # 渲染（_09）
    "render_tree",
    "source_summary",
    "UNKNOWN_SOURCE",
]
