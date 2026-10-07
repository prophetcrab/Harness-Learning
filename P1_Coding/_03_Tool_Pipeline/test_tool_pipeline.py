"""_03_Tool_Pipeline 的验收测试：单元级、全离线、不需要 key。

覆盖学习计划 M2 的三件套验收 + _03 的新能力：
1. 注册表：定义与实现分离、全局重名 fail loud、作用域遮蔽 + 撤下后重新可见
2. schema：pydantic → JSON Schema（required / 默认值 / 剥掉 title）
3. 审批三态：allow 放行 / deny 短路（执行器不被调用）/ ask 走审批策略
4. fail-closed：无审批策略、非交互终端，一律拒绝
5. 超时：execute 段超时返回 TOOL_TIMEOUT，不吞掉循环
6. post 加工：中间件能替换结果（截断）
7. 参数改写：pre 中间件改参数后，后续中间件与执行器都看到新值
8. 与 AgentLoop 集成：管线插进主循环，审批拒绝后模型不重试、如实收尾

运行：cd P1_Coding/_03_Tool_Pipeline && python -m pytest -q
"""

import time

import pytest
from pydantic import BaseModel, Field

from agent_loop import AgentLoop
from llm_seam import FakeLLM, text_reply, tool_call_reply
from tool_pipeline import (
    ApprovalDecision,
    AutoApprove,
    AutoDeny,
    DenyTools,
    PromptApprover,
    RequireApproval,
    ScriptedApprover,
    ToolPipeline,
    ToolRegistry,
    TruncateOutput,
    build_default_registry,
    define_tool,
    schema_from_model,
)
from tool_pipeline.pipeline import ToolResult


# =========================================================================
# 1) 注册表：定义/实现分离 + 分层遮蔽
# =========================================================================


def test_define_and_register_separates_definition_from_implementation():
    """同一份定义可以绑不同实现 —— 定义与实现是两个东西。"""
    definition = define_tool("greet", "打招呼", {"type": "object", "properties": {}})
    registry = ToolRegistry()
    registry.register(definition, lambda: {"hello": "world"})

    assert registry.resolve("greet").func() == {"hello": "world"}
    assert [s.name for s in registry.specs()] == ["greet"]


def test_global_duplicate_registration_is_loud():
    """全局层同名重复注册直接报错（fail loud，不静默覆盖）。"""
    registry = ToolRegistry()
    registry.register(define_tool("dup", "x"), lambda: {})
    with pytest.raises(ValueError, match="重名"):
        registry.register(define_tool("dup", "y"), lambda: {})


def test_scope_shadows_global_then_reveals_again():
    """作用域同名遮蔽全局；作用域撤下后，全局定义自动重新可见。"""
    registry = ToolRegistry()
    registry.register(define_tool("read", "全局只读实现"), lambda: {"source": "global"})

    scope = registry.scoped("session-1")
    scope.register(define_tool("read", "会话级实现"), lambda: {"source": "scoped"})

    # 作用域视图看到的是自己的实现
    assert scope.resolve("read").func() == {"source": "scoped"}
    assert scope.resolve("read").scope == "session-1"
    # 全局注册表本身不受影响
    assert registry.resolve("read").func() == {"source": "global"}

    # 作用域内可新增工具（不污染全局）
    scope.register(define_tool("write", "会话新增"), lambda: {"w": 1})
    assert "write" in scope.visible()
    assert "write" not in registry.visible()


# =========================================================================
# 2) schema：pydantic → JSON Schema
# =========================================================================


def test_schema_from_model_strips_title_and_keeps_constraints():
    class Args(BaseModel):
        expression: str = Field(description="表达式")
        precision: int = 2

    schema = schema_from_model(Args)

    assert schema["type"] == "object"
    assert "title" not in schema
    assert "title" not in schema["properties"]["expression"]
    assert schema["required"] == ["expression"]  # 有默认值的不是必填
    assert schema["properties"]["expression"]["description"] == "表达式"
    assert schema["properties"]["precision"]["default"] == 2


# =========================================================================
# 3) 审批三态
# =========================================================================


def _counting_tool():
    calls = {"n": 0}

    def run(**kwargs):
        calls["n"] += 1
        return {"ok": True, "kwargs": kwargs}

    return run, calls


def test_pre_deny_short_circuits_execution():
    """pre 中间件 deny → 执行器根本不被调用。"""
    registry = ToolRegistry()
    run, calls = _counting_tool()
    registry.register(define_tool("danger", "危险工具"), run)

    pipeline = ToolPipeline(registry, middlewares=[DenyTools(["danger"])])
    result = pipeline.run("danger", {})

    assert result.status == "denied"
    assert calls["n"] == 0  # 关键：短路了，没执行
    assert "denied" in result.content


def test_ask_goes_through_approval_policy():
    """needs_approval 的工具转 ask，走审批策略；批准后才执行。"""
    registry = ToolRegistry()
    run, calls = _counting_tool()
    registry.register(define_tool("write", "写操作", needs_approval=True), run)

    approver = ScriptedApprover([ApprovalDecision(True, "批准")])
    pipeline = ToolPipeline(registry, approval=approver)

    result = pipeline.run("write", {"path": "a.txt"})

    assert result.status == "ok"
    assert calls["n"] == 1
    assert approver.requests[0].tool_name == "write"
    approver.assert_all_consumed()


def test_ask_rejected_does_not_execute():
    """审批拒绝 → 不执行，返回 denied，原因回填给模型。"""
    registry = ToolRegistry()
    run, calls = _counting_tool()
    registry.register(define_tool("write", "写操作", needs_approval=True), run)

    pipeline = ToolPipeline(registry, approval=AutoDeny("用户拒绝"))
    result = pipeline.run("write", {})

    assert result.status == "denied"
    assert calls["n"] == 0
    assert "用户拒绝" in result.content["error"]


def test_require_approval_middleware_tightens_policy():
    """RequireApproval 可在不改编定义的前提下，按会话把某工具转成 ask。"""
    registry = ToolRegistry()
    run, calls = _counting_tool()
    registry.register(define_tool("read", "读操作"), run)  # 默认无需审批

    approver = ScriptedApprover([ApprovalDecision(False, "本会话禁止读取")])
    pipeline = ToolPipeline(
        registry, approval=approver, middlewares=[RequireApproval(["read"])]
    )
    result = pipeline.run("read", {})

    assert result.status == "denied"
    assert calls["n"] == 0


# =========================================================================
# 4) fail-closed
# =========================================================================


def test_no_approval_policy_is_fail_closed():
    """工具需要审批但没配审批策略 → 拒绝，而不是默认放行。"""
    registry = ToolRegistry()
    run, calls = _counting_tool()
    registry.register(define_tool("write", "写操作", needs_approval=True), run)

    pipeline = ToolPipeline(registry, approval=None)
    result = pipeline.run("write", {})

    assert result.status == "denied"
    assert calls["n"] == 0
    assert "fail-closed" in result.content["error"]


def test_prompt_approver_non_interactive_is_fail_closed():
    """非交互终端下 PromptApprover 一律拒绝。"""
    approver = PromptApprover(interactive=False)
    from tool_pipeline import ApprovalRequest

    decision = approver.approve(ApprovalRequest(tool_name="write", arguments={}))
    assert decision.allowed is False
    assert "fail-closed" in decision.reason


def test_prompt_approver_yes_no():
    """交互式下 y 放行、n 拒绝（用注入的 input_fn 离线驱动）。"""
    from tool_pipeline import ApprovalRequest

    yes = PromptApprover(input_fn=lambda _: "y", interactive=True)
    no = PromptApprover(input_fn=lambda _: "n", interactive=True)
    req = ApprovalRequest(tool_name="write", arguments={"path": "a"})
    assert yes.approve(req).allowed is True
    assert no.approve(req).allowed is False


# =========================================================================
# 5) 超时
# =========================================================================


def test_timeout_returns_structured_result():
    """执行超时返回 status=timeout 与 TOOL_TIMEOUT 码，不抛异常。"""
    registry = ToolRegistry()
    registry.register(define_tool("slow", "慢工具"), lambda seconds: (time.sleep(seconds), {})[1])

    pipeline = ToolPipeline(registry, timeout=0.1)
    result = pipeline.run("slow", {"seconds": 2.0})

    assert result.status == "timeout"
    assert result.content["code"] == "TOOL_TIMEOUT"


def test_fast_tool_within_timeout_succeeds():
    registry = ToolRegistry()
    registry.register(define_tool("fast", "快工具"), lambda: {"done": True})
    pipeline = ToolPipeline(registry, timeout=5.0)
    assert pipeline.run("fast", {}).status == "ok"


# =========================================================================
# 6) post 加工
# =========================================================================


def test_post_middleware_truncates_long_output():
    registry = ToolRegistry()
    registry.register(define_tool("big", "大输出"), lambda: {"text": "x" * 5000})

    pipeline = ToolPipeline(registry, middlewares=[TruncateOutput(max_chars=100)])
    result = pipeline.run("big", {})

    assert result.status == "ok"
    assert result.content["truncated"] is True
    assert len(result.content["text"]) < 5000
    assert "已截断" in result.content["text"]


# =========================================================================
# 7) 参数改写
# =========================================================================


def test_pre_middleware_can_rewrite_arguments():
    """pre 改写参数后，执行器看到的是新值。"""
    from tool_pipeline.pipeline import PreDecision

    class NormalizePath:
        def pre(self, ctx):
            if ctx.tool_name == "read" and not ctx.arguments["path"].startswith("work/"):
                return PreDecision("allow", arguments={"path": "work/" + ctx.arguments["path"]})
            return None

    registry = ToolRegistry()
    seen = {}
    registry.register(define_tool("read", "读"), lambda path: seen.update(path=path) or {"path": path})

    pipeline = ToolPipeline(registry, middlewares=[NormalizePath()])
    result = pipeline.run("read", {"path": "notes.txt"})

    assert result.content["path"] == "work/notes.txt"
    assert seen["path"] == "work/notes.txt"


# =========================================================================
# 8) 未知工具 + 与 AgentLoop 集成
# =========================================================================


def test_unknown_tool_is_structured_result():
    registry = ToolRegistry()
    pipeline = ToolPipeline(registry)
    result = pipeline.run("nope", {})
    assert result.status == "unknown_tool"
    assert "未知工具" in result.content["error"]


def test_pipeline_plugs_into_agent_loop_with_approval_denied(tmp_path):
    """管线插进 AgentLoop：审批拒绝后模型拿到拒绝原因、不重试、如实收尾。"""
    registry = build_default_registry(tmp_path)

    provider = FakeLLM(
        [
            tool_call_reply("write_file", {"path": "x.txt", "content": "hi"}),
            text_reply("写入被拒绝，我没有权限，无法完成。"),
        ]
    )
    pipeline = ToolPipeline(registry, approval=AutoDeny("用户拒绝写入"))
    loop = AgentLoop(provider, pipeline.execute, system_prompt="测试助手")

    result = loop.run("写个文件", tools=registry.specs())

    assert result.status == "done"
    tool_step = result.steps[0]
    assert tool_step.tool_results[0].error is True
    assert "用户拒绝写入" in tool_step.tool_results[0].result["error"]
    # 确认没有真的写文件
    assert not (tmp_path / "x.txt").exists()
    provider.assert_all_consumed()


def test_pipeline_plugs_into_agent_loop_approved_path(tmp_path):
    """管线插进 AgentLoop：审批通过后工具真正执行，结果回填。"""
    registry = build_default_registry(tmp_path)

    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "6*7"}),
            text_reply("答案是 42"),
        ]
    )
    pipeline = ToolPipeline(registry, approval=AutoApprove())
    loop = AgentLoop(provider, pipeline.execute)

    result = loop.run("6*7", tools=registry.specs())

    assert result.status == "done"
    assert result.steps[0].tool_results[0].result["result"] == 42
    provider.assert_all_consumed()


def test_workspace_sandbox_rejects_escape(tmp_path):
    """write_file 沙箱：路径越界直接拒绝（审批通过也不放行）。"""
    registry = build_default_registry(tmp_path)
    pipeline = ToolPipeline(registry, approval=AutoApprove())  # 就算审批放行

    result = pipeline.run("write_file", {"path": "../escape.txt", "content": "x"})

    assert result.status == "ok"  # 工具自己返回结构化错误（不是管线拒绝）
    assert "越出工作区" in result.content["error"]
