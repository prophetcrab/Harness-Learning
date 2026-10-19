"""真实模型装配单：接 DeepSeek（OpenAI 兼容接口）。

与 `minimal` 的唯一区别是模型后端那一行：

    minimal   llm-provider + 本地剧本 transport（离线）
    realtime  llm-provider + 真实 base_url（按环境变量取密钥）

**凭证只从环境变量来**（`DEEPSEEK_API_KEY`），装配单里不出现密钥。
`app.py` 会在启动前加载项目根的 `.env`。

模型名与端点也从环境变量取（`DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL`），
没配就用下面的默认值。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from manifests.minimal import catalog as _offline_catalog
from manifests.minimal import manifest as _offline_manifest

#: 默认端点与模型（环境变量可覆盖）。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

#: 模型能力（按 DeepSeek 文档；端点若换模型，装配单里改这里）。
CONTEXT_WINDOW = 128_000
MAX_OUTPUT_TOKENS = 8_192


def catalog() -> dict[str, Any]:
    """目录与离线版完全相同——适配器是同一个，只是配置不同。"""
    return _offline_catalog()


def manifest(
    *,
    workspace: str | Path = "workspace",
    sessions: str | Path = "sessions",
    sandbox_mode: str = "changes",
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str = "DEEPSEEK_API_KEY",
    approval_policy: str = "ask",
) -> list[dict[str, Any]]:
    """真实模型的最小装配单。

    与 `minimal.manifest()` 的差别：`llm-provider` 用真实端点与密钥环境变量，
    没有剧本 transport。
    """
    endpoint = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    model_id = model or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL

    entries = _offline_manifest(
        workspace=workspace,
        sessions=sessions,
        sandbox_mode=sandbox_mode,
        approval_policy=approval_policy,
    )
    for entry in entries[0]["children"]:
        if entry["id"] != "llm-provider":
            continue
        entry["config"] = {
            "route": "deepseek",
            "base_url": endpoint,
            "api_key_env": api_key_env,
            "models": [
                {
                    "id": model_id,
                    "context_window": CONTEXT_WINDOW,
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                }
            ],
            "timeout": 120,
        }
    # 默认路由跟着换成真实路由
    for entry in entries[0]["children"]:
        if entry["id"] == "llm":
            entry["config"] = {"default_provider": "deepseek", "default_model": model_id}
    return entries


__all__ = [
    "CONTEXT_WINDOW",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "MAX_OUTPUT_TOKENS",
    "catalog",
    "manifest",
]
