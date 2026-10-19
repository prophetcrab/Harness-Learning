"""真实 API 测试的开关。

默认门禁（`python -m pytest tests -q`）必须保持离线：真实 API 用例一律
带 `real` 标记，只有显式 `--real`（或 `P4_REAL_API=1`）才收集运行。
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--real",
        action="store_true",
        default=False,
        help="运行会调用真实 LLM API 的功能验收（联网、计费）",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real: 调用真实 LLM API 的功能验收（默认跳过）"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--real") or os.environ.get("P4_REAL_API") == "1":
        return
    skip = pytest.mark.skip(
        reason="真实 API 用例默认跳过：pytest --real（或设置 P4_REAL_API=1）"
    )
    for item in items:
        if "real" in item.keywords:
            item.add_marker(skip)
