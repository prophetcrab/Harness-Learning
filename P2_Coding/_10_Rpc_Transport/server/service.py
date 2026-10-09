"""服务层 —— HarnessService：把 harness 包成一个可被连接的**常驻服务**。

M7 的核心：不再"启动一个程序跑一次对话"，而是"起一个服务，客户端连上来用 RPC
调用它"。本阶段只做两个方法（一次只加一个机制；事件流跟随是 `_11` 的事）：

    initialize       握手：交换版本与能力清单（工具名单）；之后的调用才被受理
    session.prompt   跑一个 turn（`{"session": "s1", "text": "..."}`）；
                     会话按 id 缓存——同名继续同一场（turn 接续），新名新开一场
                     （与 CLI 的 `--session` 同一语义）

三条设计：

1. **握手门禁**：`initialize` 之前调用其它方法 → `NOT_INITIALIZED`（-32002）。
   服务化之后"谁在连、能不能用"不再靠人盯着终端；握手把"版本对不对、有没有我要的
   能力"变成协议里可检查的一步（dsh 的 `initialize` 同理）。
2. **会话缓存**：服务常驻，同一 session 的下一句 prompt 应当接在上一句后面——
   缓存 `session id → ContextHarness`，底层仍是"打开即恢复"的日志语义（进程重启、
   缓存清空后重连同名会话，历史照样从 JSONL 恢复）。
3. **无交互应答方**：服务端没有人可以点 y/n——默认 **fail-closed**（全部拒绝），
   要放开必须显式传 `approval=AutoApprove()`（服务化的审批纪律：默认安全）。

服务不直接碰传输：它只认"方法名 + 参数字典"，由 transport 层负责帧的收发。
因此本模块可以在任何传输（stdio / 测试里的 StringIO / 未来的 HTTP）上复用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from context import collect_runtime_context, open_context_harness
from harness.tools import ApprovalPolicy
from harness.tools.approval import AutoDeny
from kernel import Context
from providers import LLM_CAPABILITY, TOOLBOX_CAPABILITY
from server.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, NOT_INITIALIZED, RpcError

#: 协议版本与服务器标识（initialize 响应的一部分；golden 测试钉住）。
PROTOCOL_VERSION = "harness-jsonrpc/1"
SERVER_NAME = "mini-harness"
SERVER_VERSION = "0.2.0"

#: 全部可用方法（写进 initialize 的能力清单，也是未知方法报错时的提示来源）。
METHODS = ("initialize", "session.prompt")


class HarnessService:
    """基于一个已 boot 的 ctx 提供 RPC 方法（会话按需打开并缓存）。"""

    def __init__(
        self,
        plug_ctx: Context,
        *,
        root: str | Path,
        workspace: str | Path | None = None,
        approval: ApprovalPolicy | None = None,
    ) -> None:
        self._ctx = plug_ctx
        self._root = Path(root)
        self._workspace = Path(workspace) if workspace is not None else Path(root)
        # 默认 fail-closed：服务端没有人工应答方，拒绝一切需审批操作。
        self._approval: ApprovalPolicy = approval or AutoDeny(
            "RPC 服务无交互应答方：未授权任何需审批操作（fail-closed）"
        )
        self._initialized = False
        self._sessions: dict[str, Any] = {}  # session id → ContextHarness

    # ------------------------------------------------------------------
    # 方法分派
    # ------------------------------------------------------------------

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """按方法名分派；返回可序列化的结果（服务层的错误以 RpcError 抛出）。"""
        if method == "initialize":
            return self.initialize(params)
        if method == "session.prompt":
            return self.session_prompt(params)
        raise RpcError(
            METHOD_NOT_FOUND, f"未知方法：{method!r}（可用：{'、'.join(METHODS)}）"
        )

    # ------------------------------------------------------------------
    # initialize：握手
    # ------------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手：返回协议版本、服务器标识与能力清单。

        params 暂无必需项（保留给客户端自报身份）；重复 initialize 幂等
        （返回同一份信息——服务端把"再握一次手"当作良性重试而不是错误）。
        """
        self._initialized = True
        tools = sorted(spec.name for spec in self._ctx.require(TOOLBOX_CAPABILITY).specs())
        return {
            "protocol": PROTOCOL_VERSION,
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"methods": list(METHODS), "tools": tools},
        }

    # ------------------------------------------------------------------
    # session.prompt：跑一个 turn
    # ------------------------------------------------------------------

    def session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """跑一个 turn：`{"session": "s1", "text": "..."}` → 结果摘要。

        校验（全部 fail loud，参数问题统一 INVALID_PARAMS）：
        - 未握手 → NOT_INITIALIZED；
        - `text` 必填且非空字符串；`session` 可选（默认 "default"）且必须是非空字符串。
        """
        if not self._initialized:
            raise RpcError(
                NOT_INITIALIZED, "服务尚未初始化：请先调用 initialize 完成握手"
            )
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError(INVALID_PARAMS, "session.prompt 需要非空的 text 参数")
        session_id = params.get("session", "default")
        if not isinstance(session_id, str) or not session_id.strip():
            raise RpcError(INVALID_PARAMS, "session 必须是非空字符串")

        harness = self._open_session(session_id)
        result = harness.harness.send(text)
        return {
            "session": session_id,
            "turn": result.turn,
            "status": result.status,
            "steps": len(result.steps),
            "final_text": result.final_text,
        }

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def _open_session(self, session_id: str):
        """同名复用缓存，新名新开一场（打开即恢复——日志语义在底层兜底）。"""
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        llm = self._ctx.require(LLM_CAPABILITY)
        registry = self._ctx.require(TOOLBOX_CAPABILITY)
        harness = open_context_harness(
            session_id,
            provider=llm,
            root=self._root,
            workspace=self._workspace,
            approval=self._approval,
            context_source=lambda: collect_runtime_context(self._workspace),
            tool_registry=registry,
        )
        self._sessions[session_id] = harness
        return harness

    @property
    def open_sessions(self) -> list[str]:
        """当前缓存的会话 id（按打开顺序；观测/测试用）。"""
        return list(self._sessions)


__all__ = [
    "HarnessService",
    "METHODS",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
]
