"""pytest 公共夹具。"""

from __future__ import annotations

import pytest

from core import Context


@pytest.fixture()
def ctx() -> Context:
    return Context()
