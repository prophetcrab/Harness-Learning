"""agent-admin —— 不同的 agent 模式（基础插件）。

一个**模式**就是一套 agent 配置预设（模型路由 / 步数 / 预算 / 角色）。它让
"用哪个模型、跑多长、什么角色"成为**数据**，而不是散在调用点的参数里。

    admin.modes()                       有哪些模式
    admin.describe(name) -> AgentMode   模式的完整配置
    admin.set_default(name)             新 agent 的默认模式
    admin.create(mode="careful", session_id=...) -> AgentHandle
    admin.stop(agent_id)                停掉一个 agent
    admin.list() / snapshot()           当前在跑的 agent

**内置三档**（装配单可覆盖 / 追加）：

    default   不额外限制（模型走 llm 的装配默认）
    quick     max_steps=3（一次性小任务，防止跑飞）
    careful   max_steps=30 + token_budget（长任务，有上限）

**必要依赖**：`agent`（注册表）与 `agent-loop`（驱动器）——没有它们，
"创建 agent"这件事不成立，所以在装配期就检查。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core import Context


# === 本地副本（来自 plugins/base/agent.py；插件之间不 import，见决策 0053）===
@dataclass(frozen=True)
class AgentOptions:
    """创建一个 agent 时的配置（模式就是它的预设）。

    全部可选：没给的路由用会话记录里的选择，再退到装配默认（0044）。
    """

    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    token_budget: int | None = None
    #: 模式名（agent-admin 用来分组 / 展示；驱动器不解释它）。
    mode: str | None = None
    #: 驱动器私有的附加选项（不参与通用校验）。
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError(f"max_steps 必须是 None 或 >= 1，收到：{self.max_steps}")
        if self.token_budget is not None and self.token_budget < 1:
            raise ValueError(f"token_budget 必须是 None 或 >= 1，收到：{self.token_budget}")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError(f"max_tokens 必须是 None 或 >= 1，收到：{self.max_tokens}")

    def merged(self, **overrides: Any) -> AgentOptions:
        """以自己为底，覆盖非 None 的字段（模式 → 显式参数的合并）.。"""
        data = {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "max_steps": self.max_steps,
            "token_budget": self.token_budget,
            "mode": self.mode,
            "extra": dict(self.extra),
        }
        for key, value in overrides.items():
            if value is not None:
                data[key] = value
        return AgentOptions(**data)

# --- 服务/事件名（字符串，本地复刻）---
AGENT_SERVICE = "agent"



#: 管理服务的名字。
AGENT_ADMIN_SERVICE = "agent-admin"

@dataclass(frozen=True)
class AgentMode:
    """一套 agent 配置预设。"""

    name: str
    description: str = ""
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    token_budget: int | None = None
    #: 可选：创建后给这个会话设置的 character-admin 角色名。
    character: str | None = None

    def options(self) -> AgentOptions:
        return AgentOptions(
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            max_steps=self.max_steps,
            token_budget=self.token_budget,
            mode=self.name,

        )
    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "model": self.model,
            "max_steps": self.max_steps,
            "token_budget": self.token_budget,
            "character": self.character,
        }

#: 内置模式（装配单可以整段覆盖）。
BUILTIN_MODES: tuple[AgentMode, ...] = (
    AgentMode(
        name="default",
        description="日常：交给驱动器的默认上限（安全上限 64 步）",
        max_steps=None,
    ),
    AgentMode(
        name="quick",
        description="快问快答：最多 3 步",
        max_steps=3,
    ),
    AgentMode(
        name="careful",
        description="长任务：最多 30 步、10 万 token 预算",
        max_steps=30,
        token_budget=100_000,
    ),
)


class AgentAdminService:
    """模式表 + 一组便捷动作（创建 / 停止 / 列举）。"""

    def __init__(
        self,
        ctx: Context,
        registry: AgentRegistry,
        *,
        modes: Mapping[str, AgentMode] | None = None,
        default: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._registry = registry
        self._modes: dict[str, AgentMode] = {mode.name: mode for mode in BUILTIN_MODES}
        for name, mode in (modes or {}).items():
            if mode.name != name:
                mode = AgentMode(
                    name=name,
                    description=mode.description,
                    provider=mode.provider,
                    model=mode.model,
                    reasoning_effort=mode.reasoning_effort,
                    max_tokens=mode.max_tokens,
                    max_steps=mode.max_steps,
                    token_budget=mode.token_budget,
                    character=mode.character,
                )
            self._modes[name] = mode
        self._default = default or "default"
        if self._default not in self._modes:
            raise ValueError(
                f"默认模式不存在：{self._default!r}（可用：{'、'.join(self.modes())}）"
            )

    # ------------------------------------------------------------------
    # 模式
    # ------------------------------------------------------------------

    def modes(self) -> tuple[str, ...]:
        return tuple(self._modes)

    def describe(self, name: str) -> AgentMode:
        mode = self._modes.get(name)
        if mode is None:
            raise ValueError(
                f"模式不存在：{name!r}（可用：{'、'.join(self.modes())}）"
            )
        return mode

    @property
    def default_mode(self) -> str:
        return self._default

    def set_default(self, name: str) -> None:
        self.describe(name)  # 校验
        self._default = name

    def define(self, mode: AgentMode) -> None:
        """追加或整段覆盖一个模式。"""
        if not isinstance(mode.name, str) or not mode.name:
            raise ValueError("模式名必须是非空字符串")
        self._modes[mode.name] = mode

    def remove(self, name: str) -> None:
        if name == self._default:
            raise ValueError(f"{name!r} 是默认模式，不能删除（先 set_default 别的）")
        if name not in self._modes:
            raise ValueError(f"模式不存在：{name!r}")
        del self._modes[name]

    # ------------------------------------------------------------------
    # agent 生命周期
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        mode: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        parent: str | None = None,
        **overrides: Any,
    ) -> AgentHandle:
        """按模式创建 agent；`overrides` 覆盖模式里的同名字段。"""
        preset = self.describe(mode or self._default)
        options = preset.options().merged(**overrides)
        handle = self._registry.create(
            options=options, session_id=session_id, agent_id=agent_id, parent=parent
        )
        if preset.character:
            characters = self._ctx.get("character-admin")
            if characters is None:
                handle.dispose()
                raise RuntimeError(
                    f"模式 {preset.name!r} 需要 character-admin，但它没有装载"
                )
            characters.set(handle.session_id, preset.character)
        return handle

    def resume(
        self,
        session_id: str,
        *,
        mode: str | None = None,
        agent_id: str | None = None,
        parent: str | None = None,
        **overrides: Any,
    ) -> AgentHandle:
        preset = self.describe(mode or self._default)
        options = preset.options().merged(**overrides)
        return self._registry.resume(
            session_id, options=options, agent_id=agent_id, parent=parent

        )
    def stop(self, agent_id: str) -> bool:
        """停掉一个 agent（释放句柄）；不存在 → False。"""
        handle = self._registry.get(agent_id)
        if handle is None:
            return False
        handle.dispose()
        return True

    def list(self) -> list[AgentHandle]:
        return self._registry.list()

    def get(self, agent_id: str) -> AgentHandle | None:
        return self._registry.get(agent_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "modes": [self._modes[name].as_dict() for name in self.modes()],
            "default_mode": self._default,
            "agents": self._registry.snapshot(),
        }

class AgentAdminPlugin:
    """提供 `agent-admin` 服务。装配单里应声明 `role="base"`。"""

    name = "agent-admin"
    requires = ("agent", "agent-loop")

    def __init__(
        self,
        *,
        modes: Mapping[str, AgentMode] | None = None,
        default: str | None = None,
    ) -> None:
        self._modes = dict(modes or {})
        self._default = default

    def setup(self, ctx: Context) -> None:
        registry: AgentRegistry = ctx.require(AGENT_SERVICE)
        ctx.provide(
            AGENT_ADMIN_SERVICE,
            AgentAdminService(
                ctx,
                registry,
                modes=self._modes,
                default=self._default,
            ),
        )


def agent_admin_plugin(**config: Any) -> AgentAdminPlugin:
    """工厂：装配单的 `config` 可给 `modes` / `default`。"""
    return AgentAdminPlugin(**config)

__all__ = [
    "AGENT_ADMIN_SERVICE",
    "BUILTIN_MODES",
    "AgentAdminPlugin",
    "AgentAdminService",
    "AgentMode",
    "agent_admin_plugin",
]

# --- 本地类型别名（插件之间不 import）---
AgentHandle = Any
AgentRegistry = Any
