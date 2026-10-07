"""环境与 provider 构造 —— 唯一的"选供应商"落点。

`harness.cli` 与 `demo.py` 都从这里拿 provider，于是"换供应商只改一处"（接缝的价值）
在 mini harness 里依然成立：假 provider 与真实 provider 在上层完全无差别。
"""

from __future__ import annotations

import os
from pathlib import Path

from harness.llm.provider import LLMProvider


def load_env(project_root: Path) -> None:
    """把项目根 .env 里的键值读进环境变量（已存在的不覆盖）。"""
    env_file = project_root / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def build_deepseek_provider(project_root: Path) -> LLMProvider:
    """构造真实的 DeepSeek provider；缺 key 直接 fail loud。"""
    load_env(project_root)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "未找到 DEEPSEEK_API_KEY。\n"
            "配置：项目根目录 .env 写入 DEEPSEEK_API_KEY=sk-...\n"
            "离线体验请加 --fake（不需要 key）。"
        )
    from harness.llm.deepseek import DeepSeekProvider

    return DeepSeekProvider(
        api_key=api_key,
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )


__all__ = ["load_env", "build_deepseek_provider"]
