"""步 1 验收：服务槽位（provide / get / require / retract）。

对应 PLAN.md「M1 拆步」第 1 步。语义对照 P3 `kernel/context.py`（只对照，
不搬代码——决策 0043）。
"""

from __future__ import annotations

import pytest

from core import Context, DuplicateSlotError, MissingServiceError

# ---------------------------------------------------------------------------
# 提供 / 查找
# ---------------------------------------------------------------------------


def test_provide_require_roundtrip():
    ctx = Context()
    ctx.provide("greeting", "你好")
    assert ctx.require("greeting") == "你好"
    assert ctx.services == ["greeting"]


def test_provide_can_pass_any_object():
    ctx = Context()
    service = object()
    ctx.provide("fs", service)
    assert ctx.require("fs") is service


def test_get_returns_default_for_missing_slot():
    ctx = Context()
    assert ctx.get("nothing") is None
    assert ctx.get("nothing", "默认") == "默认"


def test_services_follows_provide_order():
    ctx = Context()
    ctx.provide("b", 1)
    ctx.provide("a", 2)
    assert ctx.services == ["b", "a"]


# ---------------------------------------------------------------------------
# fail loud
# ---------------------------------------------------------------------------


def test_duplicate_provide_fails_loud_and_keeps_original():
    ctx = Context()
    original = object()
    ctx.provide("fs", original)
    with pytest.raises(DuplicateSlotError):
        ctx.provide("fs", object())
    # 拒绝是原子的：原值原样还在，服务表没被搅动
    assert ctx.require("fs") is original
    assert ctx.services == ["fs"]


def test_require_missing_fails_loud_and_is_key_error():
    ctx = Context()
    with pytest.raises(MissingServiceError):
        ctx.require("nothing")
    # 与 dict 的直觉一致：except KeyError 也能接住
    with pytest.raises(KeyError):
        ctx.require("nothing")


def test_retract_missing_fails_loud():
    ctx = Context()
    with pytest.raises(MissingServiceError):
        ctx.retract("nothing")


def test_empty_key_fails_loud():
    ctx = Context()
    with pytest.raises(ValueError):
        ctx.provide("", object())
    with pytest.raises(ValueError):
        ctx.provide(None, object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 撤销与显式替换
# ---------------------------------------------------------------------------


def test_retract_returns_value_and_removes_slot():
    ctx = Context()
    ctx.provide("greeting", "你好")
    assert ctx.retract("greeting") == "你好"
    assert ctx.services == []
    assert ctx.get("greeting") is None


def test_explicit_swap_is_retract_then_provide():
    """换实现没有 set()：先 retract 再 provide（拒绝与替换都显式）。"""
    ctx = Context()
    ctx.provide("llm", "v1")
    ctx.retract("llm")
    ctx.provide("llm", "v2")
    assert ctx.require("llm") == "v2"


def test_services_snapshot_is_a_copy():
    """`services` 给的是快照：改它不影响容器（不然调用方能偷偷改台账）。"""
    ctx = Context()
    ctx.provide("a", 1)
    snapshot = ctx.services
    snapshot.append("b")
    assert ctx.services == ["a"]
