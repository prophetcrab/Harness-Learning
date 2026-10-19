"""manifests.load —— YAML 装配单读取器（数据进、条目列表出）。

**分工**（0042 / 0054）：

    装配单 = 数据     可以用 YAML 写，改产品形态不改代码
    目录 catalog = 代码   name → 工厂的**白名单**；YAML 不能凭空装出代码

所以本模块只做三件事：读文件、替换部署变量、校验形状。它**不 import 任何
插件**，也不知道有哪些插件名——把结果交给 `core.boot(entries, catalog=...)`。

**安全**：用 `yaml.safe_load`，YAML 里不能构造任意 Python 对象。占位符只做
字符串替换，不执行表达式。

**占位符**（可出现在任何字符串值里）：

    ${workspace}                来自调用方传入的 variables
    ${env:NAME}                 环境变量（缺失 → fail loud）
    ${env:NAME:-默认值}          环境变量，缺失时用默认值

整串就是一个占位符时保留原类型（`${count}` → 10 仍是 int）；否则按字符串
拼接。未知占位符一律报错——拼错的变量名不该静默变成空串。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

#: 装配单条目允许出现的字段（与 loader 的 `_ENTRY_KEYS` 一致）。
_ENTRY_KEYS = frozenset({"id", "name", "config", "role", "enabled", "children"})

_PLACEHOLDER = re.compile(r"\$\{([^{}]+)\}")


class ManifestFileError(RuntimeError):
    """YAML 装配单读取 / 校验失败（fail loud，带文件与位置）。"""


def load_manifest(
    path: str | Path,
    *,
    variables: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """读一份 YAML 装配单，返回 launcher 可直接消费的条目列表。

    `variables` 是部署决策（工作区 / 会话目录 / 沙箱模式……），不是插件逻辑。
    """
    file = Path(path)
    if not file.is_file():
        raise ManifestFileError(f"装配单不存在：{file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestFileError(f"YAML 解析失败：{file}\n{exc}") from exc
    except OSError as exc:
        raise ManifestFileError(f"读不了装配单：{file}（{exc}）") from exc

    if data is None:
        data = []
    if not isinstance(data, list):
        raise ManifestFileError(
            f"装配单顶层必须是条目列表（`- id: ...`），收到：{type(data).__name__}（{file}）"
        )

    resolved = _resolve(data, variables or {}, file=file, where="顶层")
    _validate(resolved, file=file, where="顶层")
    return resolved


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------


def _resolve(value: Any, variables: Mapping[str, Any], *, file: Path, where: str) -> Any:
    if isinstance(value, str):
        return _resolve_string(value, variables, file=file, where=where)
    if isinstance(value, list):
        return [
            _resolve(item, variables, file=file, where=f"{where}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        return {
            key: _resolve(item, variables, file=file, where=f"{where}.{key}")
            for key, item in value.items()
        }
    return value


def _resolve_string(text: str, variables: Mapping[str, Any], *, file: Path, where: str) -> Any:
    matches = list(_PLACEHOLDER.finditer(text))
    if not matches:
        return text
    # 整串占位符：保留原类型（列表 / 整数 / 布尔……）。
    if len(matches) == 1 and matches[0].span() == (0, len(text)):
        return _lookup(matches[0].group(1), variables, file=file, where=where)

    def replace(match: re.Match[str]) -> str:
        return str(_lookup(match.group(1), variables, file=file, where=where))

    return _PLACEHOLDER.sub(replace, text)


def _lookup(expr: str, variables: Mapping[str, Any], *, file: Path, where: str) -> Any:
    expr = expr.strip()
    if expr.startswith("env:"):
        rest = expr[len("env:") :]
        name, separator, default = rest.partition(":-")
        name = name.strip()
        if not name:
            raise ManifestFileError(f"{file}：{where} 的占位符缺少环境变量名：{expr!r}")
        value = os.environ.get(name)
        if value is None:
            if separator:
                return default
            raise ManifestFileError(
                f"{file}：{where} 需要环境变量 {name!r}（未设置；"
                f"写成 ${{env:{name}:-默认值}} 可以提供默认值）"
            )
        return value

    if expr not in variables:
        known = "、".join(sorted(variables)) or "（没有传入变量）"
        raise ManifestFileError(
            f"{file}：{where} 引用了未知部署变量 {expr!r}（可用：{known}；"
            "环境变量请写 ${env:NAME}）"
        )
    return variables[expr]


# ---------------------------------------------------------------------------
# 形状校验（语义错误交给 loader 的装配期校验；这里只拦结构）
# ---------------------------------------------------------------------------


def _validate(entries: list[Any], *, file: Path, where: str) -> None:
    for index, entry in enumerate(entries):
        spot = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise ManifestFileError(
                f"{file}：{spot} 必须是字典（条目），收到：{type(entry).__name__}"
            )
        unknown = sorted(set(entry) - _ENTRY_KEYS)
        if unknown:
            raise ManifestFileError(
                f"{file}：{spot} 有未知字段 {unknown}（允许：{sorted(_ENTRY_KEYS)}）"
            )
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise ManifestFileError(f"{file}：{spot} 缺少非空字符串 id")
        if "config" in entry and not isinstance(entry["config"], Mapping):
            raise ManifestFileError(
                f"{file}：{spot}（{entry_id}）的 config 必须是字典"
            )
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            raise ManifestFileError(
                f"{file}：{spot}（{entry_id}）的 enabled 必须是 true / false"
            )
        children = entry.get("children")
        if children is not None:
            if not isinstance(children, list):
                raise ManifestFileError(
                    f"{file}：{spot}（{entry_id}）的 children 必须是列表"
                )
            _validate(children, file=file, where=f"{spot}.children")


__all__ = ["ManifestFileError", "load_manifest"]
