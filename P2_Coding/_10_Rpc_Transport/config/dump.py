"""dump —— 把最终配置树渲染成人能读的清单，**每项带来源**（`_09` 的核心产物）。

`--dump-config` 回答两个问题：

1. **最终是什么**：每一行、每个字段的最终值（行序 = 激活顺序）；
2. **它从哪来**：每个字段后面的 `← 层标签`——来自 base.yaml？dev.yaml？
   用户补丁？CLI 第几层？（来源由 `ConfigTree.sources` 台账提供，
   `load_tree` / `apply_patch` 在数据流经的每一步把它填起来。）

为什么值得单独一层：配置分层的代价就是"值散在多处"——没有来源标注，
排查"这个参数到底谁定的"要靠记忆；有了它，一眼读出每一行的来历，
`_08` 主打的"一行 patch 换 provider"也因此可验证（改一行后 dump 里那行
的来源就该变成 CLI 补丁）。

渲染格式（刻意平实、便于 grep 与快照测试）：

    【profile = dev（最终配置树）】4 行（行序 = 激活顺序）
      1. llm
           name = llm:fake            ← dev.yaml
           config = {}
      2. fs
           name = fs:memory           ← dev.yaml
           config = {}
      4. toolbox
           name = toolbox             ← base.yaml
           config.workspace = demo_workspace/ws  ← base.yaml
           config.shell_timeout = 15  ← user.patch.yaml
"""

from __future__ import annotations

from config.rows import ConfigTree

# 台账缺该项时的占位（例如纯数据构造的树没走加载器）。
UNKNOWN_SOURCE = "（未知来源）"


def _src(tree: ConfigTree, row_id: str, column: str) -> str:
    return tree.source_of(row_id, column) or UNKNOWN_SOURCE


def render_tree(tree: ConfigTree, *, title: str = "最终配置树") -> str:
    """把配置树渲染成多行文本（含每项来源）。返回字符串，不打印。"""
    lines: list[str] = [
        f"【{title}】共 {len(tree.rows)} 行（行序 = 激活顺序；"
        f"激活 {len(tree.active_rows())} 行）"
    ]

    def line(label: str, value: str, source: str) -> str:
        return f"       {label:<30} = {value:<24} ← {source}"

    for index, row in enumerate(tree.rows, start=1):
        mark = "（已禁用）" if row.disabled else ""
        lines.append(f"  {index}. {row.id}{mark}")
        lines.append(line("name", row.name, _src(tree, row.id, "name")))
        if row.config:
            config_source = _src(tree, row.id, "config")
            for key, value in row.config.items():
                lines.append(line(f"config.{key}", str(value), config_source))
        else:
            lines.append(line("config", "{}", _src(tree, row.id, "config")))
        if row.disabled:
            lines.append(line("disabled", "True", _src(tree, row.id, "disabled")))
    return "\n".join(lines)


def source_summary(tree: ConfigTree) -> dict[str, list[str]]:
    """按层汇总：每层贡献了哪些"行.字段"（从另一个角度看同一份台账）。"""
    summary: dict[str, list[str]] = {}
    for row in tree.rows:
        entry = tree.sources.get(row.id, {})
        for column in ("name", "config", "disabled"):
            label = entry.get(column)
            if label is not None:
                summary.setdefault(label, []).append(f"{row.id}.{column}")
    return summary


__all__ = ["render_tree", "source_summary", "UNKNOWN_SOURCE"]
