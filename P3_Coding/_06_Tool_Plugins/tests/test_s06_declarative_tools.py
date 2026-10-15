"""声明式工具（`@tool`）与审批闸门（gate）的构件测试。

`_06` 让"写一件工具"从三处（参数模型 + 定义 + 实现）缩到**一处**（一个函数），
这是"模型自己写工具"能成立的前提。这组测试钉住那条映射的边界：

- 签名 → JSON Schema（类型 / 必填 / 默认值 / 描述）；
- 认不出的注解、`*args`、`**kwargs` **当场报错**（不让 schema 与实现悄悄不一致）；
- 审批闸门：管线问过就不再问第二次（否则一次操作两张卡片）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from components.tools import ToolRegistry, tool
from components.tools.approval import ApprovalRequest, AutoApprove, ScriptedApprover
from components.tools.declarative import schema_from_signature
from components.tools.gate import is_preapproved, preapproved, source_approval

# ---------------------------------------------------------------------------
# 签名 → schema
# ---------------------------------------------------------------------------


def test_signature_becomes_a_clean_schema():
    @tool("demo", description="演示工具")
    def demo(name: str, count: int = 3, ratio: float = 0.5, flag: bool = False,
             tags: list[str] | None = None, meta: dict | None = None) -> dict:
        return {}

    params = demo.definition.parameters
    assert params["type"] == "object"
    assert params["properties"]["name"] == {"type": "string"}
    assert params["properties"]["count"] == {"type": "integer", "default": 3}
    assert params["properties"]["ratio"] == {"type": "number", "default": 0.5}
    assert params["properties"]["flag"] == {"type": "boolean", "default": False}
    assert params["properties"]["tags"] == {"type": "array", "items": {"type": "string"},
                                            "default": None}
    assert params["properties"]["meta"] == {"type": "object", "default": None}
    assert params["required"] == ["name"]          # 只有没默认值的算必填
    # 说明来自 description 参数（显式给了就用它）
    assert demo.definition.description == "演示工具"


def test_docstring_becomes_the_description():
    @tool("greet")
    def greet(who: str) -> dict:
        """向某人问好。

        第二段不会出现在 description 里。
        """
        return {"hello": who}

    assert greet.definition.description.startswith("向某人问好")
    assert "第二段" in greet.definition.description      # 整段文档串（含空行的后续）


def test_review_path_arg_flows_through():
    """审批时"看哪个文件"是**工具自己的声明**（`_06` 修掉盲批的机制）。"""
    @tool("danger", needs_approval=True, review_path_arg="path")
    def danger(path: str) -> dict:
        return {}

    assert danger.definition.review_path_arg == "path"
    assert danger.definition.needs_approval is True


def test_needs_approval_flag_flows_through():
    @tool("danger", needs_approval=True)
    def danger(x: str) -> dict:
        return {}

    assert danger.definition.needs_approval is True
    assert danger.definition.spec().name == "danger"


def test_unsupported_annotation_fails_loud():
    """认不出的注解**当场报错**——不让"schema 与实现不一致"悄悄通过。"""
    with pytest.raises(TypeError) as excinfo:

        @tool("bad")
        def bad(when: object) -> dict:
            return {}

    assert "不支持的注解" in str(excinfo.value)

    with pytest.raises(TypeError) as excinfo:

        @tool("noargs")
        def noargs(*args) -> dict:
            return {}

    assert "逐列出" in str(excinfo.value) or "参数表" in str(excinfo.value)


def test_missing_annotation_fails_loud():
    with pytest.raises(TypeError) as excinfo:

        @tool("annot")
        def annot(path) -> dict:      # 没有注解
            return {}

    assert "类型注解" in str(excinfo.value)


def test_declarative_tool_registers_and_calls():
    @tool("add")
    def add(a: int, b: int) -> dict:
        """相加。"""
        return {"sum": a + b}

    registry = ToolRegistry()
    registry.register(add.definition, add.func, owner="demo")
    resolved = registry.resolve("add")
    assert resolved is not None
    assert resolved.func(a=2, b=3) == {"sum": 5}
    assert add(a=1, b=1) == {"sum": 2}          # 直接调用也可（测试/手工跑）


def test_schema_from_signature_handles_optional_without_default():
    def fn(path: str, limit: int | None = None) -> dict:
        return {}

    schema = schema_from_signature(fn, "fn")
    assert schema["properties"]["limit"] == {"type": "integer", "default": None}
    assert schema["required"] == ["path"]


# ---------------------------------------------------------------------------
# 审批闸门（gate）
# ---------------------------------------------------------------------------


def test_preapproved_is_thread_local_and_restored():
    assert is_preapproved("install_tool") is False
    with preapproved("install_tool"):
        assert is_preapproved("install_tool") is True
        assert is_preapproved("install_plugin") is False
    assert is_preapproved("install_tool") is False

    with preapproved("a"):
        with preapproved("b"):
            assert is_preapproved("b") is True
        assert is_preapproved("a") is True     # 内层退出后恢复外层标记


def test_source_approval_is_fail_closed_without_policy():
    decision = source_approval(None, tool_name="install_tool", path="p.py",
                               source="x = 1", reason="装")
    assert decision.allowed is False
    assert "fail-closed" in decision.reason


def test_source_approval_carries_the_source_and_the_policy_decides():
    seen: list[ApprovalRequest] = []

    class Spy:
        def approve(self, request: ApprovalRequest):
            seen.append(request)
            return AutoApprove().approve(request)

    decision = source_approval(Spy(), tool_name="install_tool", path="plugins/x.py",
                              source="print(1)\n", reason="请确认源码")
    assert decision.allowed is True
    assert seen[0].tool_name == "install_tool"
    assert seen[0].arguments["source"] == "print(1)\n"      # 审批人看到全文
    assert seen[0].arguments["bytes"] == len(b"print(1)\n")
    assert "源码" in seen[0].reason


def test_action_approval_is_used_by_mutating_tools(tmp_path: Path):
    """`action_approval` 同样 fail-closed，理由里说清要动什么。"""
    from components.tools.gate import action_approval

    denied = action_approval(None, tool_name="t", action="disable",
                             target="calculate", detail="停用工具")
    assert denied.allowed is False

    from components.tools import ApprovalDecision

    allowed = action_approval(ScriptedApprover([ApprovalDecision(True, "ok")]),
                              tool_name="t", action="disable", target="calculate",
                              detail="停用工具 calculate")
    assert allowed.allowed is True
