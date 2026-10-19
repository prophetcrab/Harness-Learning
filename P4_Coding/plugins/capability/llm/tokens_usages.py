"""llm-tokens-usages —— 消耗统计（能力子插件）：订阅 `llm/finish`，按桶记账。

用户要的六个数字，分三个维度：

                    总量            今日                本对话
    tokens          总消耗          日消耗              本对话消耗
    缓存命中率      总命中率        日命中率（**按用户**）  本对话命中率

**分桶规则**（关键设计）：

    · "本对话"按 `request.session_id` 分桶——没有 session_id 的调用不计入。
    · "今日"统一按 **应用用户**（`request.user_id`，缺省 `anonymous`）分桶：
      日用量与日命中率都是"这个用户今天"的，不是全应用共享一个数。
      用户要求"日缓存命中率按应用用户级别独立管理"——这里对所有日维度一致处理。
    · "总量"不分桶（进程内累计；落盘/跨重启持久化留给 M5 的存储插件）。

**不侵入调用路径**：本插件只订阅事件，不改请求、不阻塞流。llm 主干在
`llm/finish` 里已经带了 request / usage；没有 usage 的调用（例如适配器没回
用量）按 0 计但**计入调用次数**，这样"有调用却零 token"能被看见。

**可查询的视图**（`snapshot()`）：

    totals / today_total / today_by_user[user] / sessions[session_id]

每个视图都含：calls / input / output / total / cached / cache_hit_rate。

**只读对外**：服务 `llm-tokens-usages` 提供的是 `TokenUsageView`——只有
`snapshot()` 和便利属性，没有 `record()`。记账只发生在插件自己的
`llm/finish` 处理器里，别的插件拿不到写入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core import Context
from plugins.base.llm import LLM_FINISH_EVENT, Usage

#: 没有用户归属时的桶名。
ANONYMOUS_USER = "anonymous"

@dataclass
class _Bucket:
    """一个统计桶（总量 / 某个用户今天 / 某个会话）。"""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def add(self, usage: Usage | None) -> None:
        self.calls += 1
        if usage is None:
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_input_tokens += usage.cached_input_tokens

    def as_dict(self) -> dict[str, Any]:
        total = self.input_tokens + self.output_tokens
        rate = (self.cached_input_tokens / self.input_tokens) if self.input_tokens else 0.0
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": total,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_hit_rate": round(min(1.0, rate), 6),
        }

@dataclass
class TokenUsageStore:
    """三维度账本：总量 + 今日（合计 + 按用户）+ 按会话。

    **写入是不对外暴露的**：`record()` 是账本自己的方法，提供给插件内部的事件
    处理器用；对外只给 `TokenUsageView`（只读快照）。这样任何插件 `require`
    到统计服务，也只能读、不能改数字。
    """

    today: date = field(default_factory=date.today)
    totals: _Bucket = field(default_factory=_Bucket)
    #: **今日合计**（所有用户加起来）——用户要求的"日消耗 tokens"。
    today_total: _Bucket = field(default_factory=_Bucket)
    #: 用户 → "今天"的桶
    users_today: dict[str, _Bucket] = field(default_factory=dict)
    #: 会话 → 桶
    sessions: dict[str, _Bucket] = field(default_factory=dict)

    def record(self, request: LLMRequest, usage: Usage | None) -> None:
        self._roll_day()
        self.totals.add(usage)
        self.today_total.add(usage)
        user = request.user_id or ANONYMOUS_USER
        self.users_today.setdefault(user, _Bucket()).add(usage)
        if request.session_id:
            self.sessions.setdefault(request.session_id, _Bucket()).add(usage)

    def snapshot(self) -> dict[str, Any]:
        self._roll_day()
        return {
            "today": self.today.isoformat(),
            "totals": self.totals.as_dict(),
            "today_total": self.today_total.as_dict(),
            "today_by_user": {
                user: bucket.as_dict() for user, bucket in self.users_today.items()
            },
            "sessions": {
                session: bucket.as_dict() for session, bucket in self.sessions.items()
            },
        }

    def _roll_day(self) -> None:
        """跨天：把"今日"的合计与用户桶清零（总量与会话桶保留）。"""
        current = date.today()
        if current != self.today:
            self.today = current
            self.today_total = _Bucket()
            self.users_today.clear()

class TokenUsageView:
    """**只读**视图：暴露给其它插件的统计面。

    只有 `snapshot()`（和它下面的 `totals` / `today` / `sessions` 便利属性），
    没有 `record()`——记账只发生在订阅 `llm/finish` 的那个处理器里。
    """

    def __init__(self, store: TokenUsageStore) -> None:
        self._store = store

    def snapshot(self) -> dict[str, Any]:
        """全部统计（总量 / 今日合计 / 今日按用户 / 按会话）。"""
        return self._store.snapshot()

    @property
    def totals(self) -> dict[str, Any]:
        return self.snapshot()["totals"]

    @property
    def today(self) -> dict[str, Any]:
        """今日合计（所有用户）。"""
        return self.snapshot()["today_total"]

    @property
    def sessions(self) -> dict[str, Any]:
        return self.snapshot()["sessions"]

    def __repr__(self) -> str:
        return "TokenUsageView(<只读>)"

class TokenUsagePlugin:
    """订阅 `llm/finish`，记账；提供 `llm-tokens-usages` 服务供管理面读取。"""

    name = "llm-tokens-usages"

    def __init__(self) -> None:
        self._store: TokenUsageStore | None = None

    def setup(self, ctx: Context) -> None:
        store = TokenUsageStore()
        self._store = store

        def on_finish(request: LLMRequest, usage: Usage | None = None, **_ignored: Any) -> None:
            store.record(request, usage)

        # 对外只给只读视图；记账入口只在插件内部（订阅 llm/finish）。
        ctx.provide("llm-tokens-usages", TokenUsageView(store))
        # 订阅随插件回卷：卸载统计插件，账本也随之消失（不留半截）。
        ctx.on(LLM_FINISH_EVENT, on_finish)

def llm_tokens_usages_plugin(**config: Any) -> TokenUsagePlugin:
    """工厂（当前无配置；保留 kwargs 以便装配单写法统一）。"""
    return TokenUsagePlugin(**config)

__all__ = [
    "ANONYMOUS_USER",
    "TokenUsagePlugin",
    "TokenUsageStore",
    "TokenUsageView",
    "llm_tokens_usages_plugin",
]

# --- 本地类型别名 ---
LLMRequest = Any
