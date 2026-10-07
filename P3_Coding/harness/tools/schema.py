"""用 pydantic 模型声明工具参数，自动生成 JSON Schema。

手写 JSON Schema 字典（_01/_02 的做法）容易出岔子：漏个 required、写错类型名
都要等真实模型调用时才暴露。这里改用 pydantic 模型声明参数，由它生成 schema ——
类型/必填/默认值/描述全在一处，还能顺带做参数校验。

对应 dsh：tools 用 schema 描述工具入参（`docs/cookbook/adding-a-tool.md`）；
本项目用 pydantic 承担同样的"声明式 schema"角色。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# pydantic 会给 schema 打上 "title" 注解（顶层与每个属性），对模型无用且是噪声；
# 这里递归剥掉，产出干净、各家 provider 都接受的 parameters 片段。
_TITLE_KEY = "title"


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _strip_titles(value)
            for key, value in node.items()
            if key != _TITLE_KEY
        }
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def schema_from_model(model: type[BaseModel]) -> dict[str, Any]:
    """把 pydantic 模型转成模型请求用的 JSON Schema（工具 parameters 片段）。"""
    return _strip_titles(model.model_json_schema())
