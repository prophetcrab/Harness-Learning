"""上下文压缩 —— 超阈值时把旧区段折叠成一条摘要（P3 `_05` 的机制）。

五条纪律：

1. **绝不改写历史**。压缩不删、不改任何已记录的事件；它只让调用方在日志尾部
   追加一条 `compact/checkpoint`，声明"前面这一段已经折叠成这句摘要"。投影
   （`derive_messages`）读到它时把旧区段换成摘要——日志是真相，投影是视图。
   重放（重新装载日志）得到的历史与在线时**同构**，这是本阶段的验收标准。
2. **成对安全**。绝不把 `assistant(tool_calls)` 和它对应的 `tool` 结果切开：
   切了下一轮请求会被 API 直接拒绝。切点从尾部往前找"不是 tool 消息"的位置。
3. **失败即放弃**（fail-soft）。摘要调用失败不改变 turn 的走向——照常继续，
   只是这次没压成；下一次超阈值时还会再试。
4. **触发稀有**。默认 90% 触发、保留 10% 尾巴、摘要 ≤5% 容量：压完落在
   15%~20%，离下一次触发很远。压缩必然打断一次前缀缓存，所以次数越少越好。
5. **当前 turn 不动**。折叠区止步于本 turn 的第一条消息（调用方传 `protect_from`）：
   刚问的问题、刚拿到的工具结果不能在"干活干到一半"时被摘要掉。
   代价写在"已知边界"里。
6. **两种触发方式共用一套机制**：
   - **自动**（90% 阈值）：切点按"保留 10% 容量"从尾部往回攒（`choose_cut`）；
   - **手动**（`/compact`，用户按下）：跳过阈值，切点改用"折到最后一轮之前"
     （`choose_manual_cut`）——1M 窗口下 10% 是 10 万 token，按预算切等于
     什么都不折，手动就失去意义。其余守卫（最后一轮、min_tail、成对安全）
     一个不少。
   两条路径的摘要请求、日志语义（`compact/checkpoint`）、投影折叠完全一致，
   只有"何时压、折到哪"不同。

三个比例、摘要请求的字符预算都在 `CompactionPolicy` 里。

**已知边界**：压缩只折"当前 turn 之前的"消息——一个 turn 内部疯长（几百次工具
往返）时压不动，因为折掉正在干的事会让模型当场失忆。这类"单个巨大工具结果"
是 `_07`（大结果搬运）的题目。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from components.context.tokens import (
    DEFAULT_CONTEXT_WINDOW,
    measure_parts,
    message_tokens,
)
from components.llm.provider import LLMProvider, LLMRequest
from components.llm.vocabulary import Message, ToolSpec, system, user

#: 触发比例：用量到容量的这个比例就开始压（留 10% 余量给下一步增长）。
DEFAULT_TRIGGER_RATIO = 0.9
#: 保留比例：从尾部往回留到约占容量的这个比例，更早的全部摘要掉。
DEFAULT_KEEP_RATIO = 0.1
#: 摘要上限：摘要自己最多占容量的这个比例（不然保留区会被摘要吃掉）。
DEFAULT_SUMMARY_RATIO = 0.05

#: 摘要消息的前缀标记（进请求的最终文本 = 前缀 + 摘要正文）。
SUMMARY_HEADER = "【早前对话摘要】"

#: 摘要指令里要求保留的四类信息（对应"继续干活需要什么"）。
SUMMARY_INSTRUCTION = """你是对话压缩器。把下面这段对话历史压缩成一份"要点备忘"，供之后继续工作使用。

必须保留（哪类有就写哪类，没有就不写）：
1. 用户的原始目标，以及当前进展到哪一步；
2. 已经做出的决定，以及被否决的做法与原因；
3. 涉及的文件路径、命令、标识符，以及关键工具结果里的实际数据（数字、报错原文）；
4. 尚未完成的事项和下一步计划。

规则：不要编造；不要复述本指令；不要寒暄；用中文分点写；只输出备忘本身。
长度控制在约 {limit} tokens（约 {chars} 字）以内。"""

#: 摘要请求的收尾指令（放在待压缩历史之后）。
SUMMARY_TAIL = "【要求】输出要点备忘。"


def compose_summary(text: str) -> str:
    """摘要正文 → 进入请求的最终文本（带前缀标记）。"""
    return f"{SUMMARY_HEADER}\n{text.strip()}"


def is_compaction_summary(message: Message) -> bool:
    """这条消息是不是上一次压缩留下的摘要（靠前缀标记识别）。

    用途见 plan_compaction 的"空折"守卫：折叠区若只剩摘要，再折一次毫无收益
    （新的摘要 ≈ 旧的摘要），却要花一次模型调用、还可能把内容越摘越薄。
    """
    return message.role == "user" and (message.content or "").startswith(SUMMARY_HEADER)


@dataclass(frozen=True)
class CompactionPolicy:
    """压缩的阈值与预算（全部集中在这里，测试只改它不改逻辑）。"""

    capacity: int = DEFAULT_CONTEXT_WINDOW
    trigger_ratio: float = DEFAULT_TRIGGER_RATIO
    keep_ratio: float = DEFAULT_KEEP_RATIO
    summary_ratio: float = DEFAULT_SUMMARY_RATIO
    #: 无论预算多紧都要原样保留的尾部消息条数（一次工具往返至少两条）。
    min_tail: int = 2
    #: 摘要请求里每条消息最多带多少字符（超长工具结果先裁再进摘要）。
    per_message_chars: int = 2000
    #: 摘要请求里正文（不含指令）最多占容量的比例——摘要请求自己不能爆窗口。
    transcript_ratio: float = 0.4

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity 必须 >= 1，收到 {self.capacity}")
        if not 0.0 < self.trigger_ratio <= 1.0:
            raise ValueError(f"trigger_ratio 必须在 (0, 1]，收到 {self.trigger_ratio}")
        if not 0.0 < self.keep_ratio < self.trigger_ratio:
            raise ValueError(
                f"keep_ratio 必须在 (0, trigger_ratio) 之间，收到 {self.keep_ratio}"
            )
        if self.summary_ratio <= 0.0:
            raise ValueError(f"summary_ratio 必须 > 0，收到 {self.summary_ratio}")
        if self.min_tail < 1:
            raise ValueError(f"min_tail 必须 >= 1，收到 {self.min_tail}")
        if self.per_message_chars < 80:
            raise ValueError(f"per_message_chars 太小：{self.per_message_chars}")

    @property
    def trigger_tokens(self) -> float:
        """用量超过它就压缩。"""
        return self.capacity * self.trigger_ratio

    @property
    def keep_tokens(self) -> float:
        """尾部原样保留区的大小。"""
        return self.capacity * self.keep_ratio

    @property
    def summary_token_limit(self) -> int:
        """摘要正文的长度上限（写进指令，并在结果里核对）。"""
        return max(1, int(self.capacity * self.summary_ratio))

    @property
    def transcript_char_budget(self) -> int:
        """摘要请求里待压缩历史最多多少字符（按中文 0.6 token/字反推）。"""
        return max(2000, int(self.capacity * self.transcript_ratio / 0.6))


@dataclass(frozen=True)
class CompactionPlan:
    """一次压缩的"账"：折叠哪一段、预计省下多少。"""

    start: int            # 折叠区起点（第一条非 system 消息的下标）
    cut: int              # 保留区起点；折叠区间是 [start, cut)
    folded: int           # 折叠的消息条数
    used: int             # 触发时的用量（实测优先，见 plan_compaction）
    used_source: str      # "actual" | "estimate"
    estimate_tokens: int  # 纯估算总额（请求构成之和）
    folded_tokens: float  # 折叠区估算 token
    kept_tokens: float    # 保留区估算 token


@dataclass
class CompactionOutcome:
    """一次成功压缩的结果：循环拿它改写内存历史 + 记一条 checkpoint。"""

    start: int
    cut: int
    folded: int
    summary: str              # 摘要正文
    before_tokens: int        # 压缩前的用量（实测优先）
    after_tokens: int         # 压缩后的估算用量
    summary_tokens: int       # 摘要本身的估算 token
    over_limit: bool          # 摘要是否超过 policy 的长度上限（只记录，不裁剪）
    trigger: str = "auto"     # "auto"（阈值触发）| "manual"（/compact）

    @property
    def message_content(self) -> str:
        """进入请求的最终文本（投影也用这一份，保证两边逐字一致）。"""
        return compose_summary(self.summary)


def first_conversation_index(messages: Sequence[Message]) -> int:
    """第一条非 system 消息的下标。

    system 不参与折叠——它的"当前生效"由 `system/message` 的遮蔽链决定
    （见 components/session/projection.py），折叠它会把遮蔽链搅乱。
    """
    for index, message in enumerate(messages):
        if message.role != "system":
            return index
    return len(messages)


def choose_cut(
    messages: Sequence[Message],
    start: int,
    policy: CompactionPolicy,
    *,
    floor: int | None = None,
) -> int:
    """从尾部往回累积到保留预算，返回保留区起点（折叠 [start, cut)）。

    这是**自动**模式的切点（90% 触发时用）：从尾部往回攒到保留预算为止。

    三个硬约束：

    - **当前 turn 不动**：`floor`（本 turn 第一条消息的下标）及其之后的一切
      永不折叠——刚问的问题、刚拿到手的工具结果不能压掉。所以保留区起点
      被钳在 floor 之前：`cut <= floor`（fold = [start, cut) 因此够不到当前 turn）。
    - **尾巴下限**：末尾至少 `min_tail` 条消息留着（floor 之后本来就够多时，
      这条自动满足）；
    - **成对安全**：切点若落在 `tool` 消息上，说明它对应的 `assistant(tool_calls)`
      会被折掉而结果被留下——往左退，宁可多留一条（可能略超保留预算）。
    """
    limit = len(messages) if floor is None else min(max(floor, start), len(messages))
    protected = len(messages) - limit           # 当前 turn 自带的条数
    required_extra = max(0, policy.min_tail - protected)
    cut = limit
    kept = 0.0
    taken = 0
    for index in range(limit - 1, start - 1, -1):
        size = message_tokens(messages[index])
        if taken >= required_extra and kept + size > policy.keep_tokens:
            break
        kept += size
        taken += 1
        cut = index
    while start < cut < limit and messages[cut].role == "tool":
        cut -= 1
    return cut


def last_turn_start(messages: Sequence[Message]) -> int:
    """最后一轮对话的起点（最后一条 user 消息的下标；没有就返回 start）。

    手动压缩用它当折叠边界（见 choose_manual_cut）：**最后一轮原样保留**，
    之前的全部折叠。注意摘要消息也是 user 角色——它是上一次压缩的产物，
    位于列表最前；只要之后还有真的用户消息，它就不会被误判成"最后一轮"。
    """
    start = first_conversation_index(messages)
    for index in range(len(messages) - 1, start - 1, -1):
        if messages[index].role == "user":
            return index
    return start


def choose_manual_cut(
    messages: Sequence[Message],
    start: int,
    policy: CompactionPolicy,
    *,
    floor: int | None = None,
) -> int:
    """**手动**模式（`/compact`）的切点：折到 floor 之前，不看保留预算。

    与自动模式的差别只有一条、但很关键：自动按"保留 10% 容量"往回攒，
    手动**不留那一段**——用户既然明确要求压缩，就把"最后一轮之前"全折掉
    （1M 窗口下 10% 是 10 万 token，按预算切等于什么都不折，手动就没意义了）。

    仍然守住的底线：最后一轮（floor）之后一条不折、末尾至少 `min_tail` 条、
    成对安全（不劈开 tool 申请与结果）。
    """
    limit = len(messages) if floor is None else min(max(floor, start), len(messages))
    cut = min(limit, len(messages) - policy.min_tail)
    if cut <= start:
        return start
    while start < cut < len(messages) and messages[cut].role == "tool":
        cut -= 1
    return cut


def plan_compaction(
    messages: Sequence[Message],
    tools: Iterable[ToolSpec],
    policy: CompactionPolicy,
    *,
    measured: int | None = None,
    protect_from: int | None = None,
    manual: bool = False,
) -> CompactionPlan | None:
    """判断"该不该压、压到哪"——返回 None 表示不用压（纯函数，可单测）。

    **触发用量取 max(估算, 实测)**：实测来自上一次响应的 `prompt_tokens`（最可信），
    但那是"上一个请求"的大小，本请求只会更大（多了一轮回复与工具结果）；
    取两者较大值是保守但安全的一侧——宁可早压一点，不要撞窗口。

    `protect_from`：本 turn 第一条对话消息的下标（循环传入）。它之后的消息
    永不折叠——压缩只折"历史 turn"（见 loop.py 的三条底线）。

    `manual=True`（`/compact` 手动触发）：**只跳过阈值检查**，切点改用
    choose_manual_cut（折到 floor 之前，不看保留预算）。其余守卫——最后一轮
    不动、min_tail、成对安全——一个不少：手动不等于"随便压"。
    """
    parts = measure_parts(messages, tools)
    estimate_total = sum(parts.values())
    used = max(estimate_total, float(measured or 0))
    if not manual and used < policy.trigger_tokens:
        return None

    start = first_conversation_index(messages)
    cut = (
        choose_manual_cut(messages, start, policy, floor=protect_from)
        if manual
        else choose_cut(messages, start, policy, floor=protect_from)
    )
    folded = cut - start
    if folded < 1:
        return None  # 全是 system / 只有当前 turn：没什么可折的
    if all(is_compaction_summary(message) for message in messages[start:cut]):
        # 折叠区里只剩上一次的摘要（"空折"）：再摘一次不会有新收益，只是
        # 白花一次调用，还可能越摘越薄。手动连按两次 /compact 会走到这里。
        return None
    folded_tokens = sum(message_tokens(message) for message in messages[start:cut])
    kept_tokens = sum(message_tokens(message) for message in messages[cut:])
    return CompactionPlan(
        start=start,
        cut=cut,
        folded=folded,
        used=int(round(used)),
        used_source="actual" if measured is not None and measured >= estimate_total else "estimate",
        estimate_tokens=int(round(estimate_total)),
        folded_tokens=folded_tokens,
        kept_tokens=kept_tokens,
    )


def _render_message(message: Message) -> str:
    """把一条消息摊成给人（也给模型）读的一行，用于摘要请求。"""
    if message.role == "assistant" and message.tool_calls:
        calls = "；".join(
            f"{call.name}({json.dumps(call.arguments, ensure_ascii=False)})"
            for call in message.tool_calls
        )
        body = f"{message.content or ''}（申请调用：{calls}）"
    elif message.role == "tool":
        body = f"结果 → {message.content}"
    else:
        body = message.content or ""
    return f"[{message.role}] {body}".strip()


def build_transcript(
    messages: Sequence[Message],
    *,
    per_message_chars: int,
    total_chars: int,
) -> str:
    """把待压缩的历史摊平成文本（摘要请求的输入）。

    为什么不把消息原样发去做摘要：折叠区里有 `tool` 消息，很多供应商要求
    "带 tool 消息就必须同时带 tools 声明"；摊平成文本谁都能读，顺带把超长
    工具结果裁掉。

    取法是**第一条 + 从最新往回填**：第一条通常装着用户的原始目标（最不能丢），
    最新的装着"刚干到哪"；中间装不下的部分明确标出"已略"——裁剪是有损的，
    但压缩本身就有损，写明比假装完整强。
    """
    if not messages:
        return ""
    head = _render_message(messages[0])[:per_message_chars]
    budget = max(0, total_chars - len(head))
    tail: list[str] = []
    used = 0
    dropped = False
    for message in reversed(messages[1:]):
        piece = _render_message(message)[:per_message_chars]
        if used + len(piece) > budget:
            dropped = True
            continue
        tail.append(piece)
        used += len(piece)
    parts = [head, *reversed(tail)]
    if dropped:
        parts.append("…（中间部分因长度限制已略）")
    return "\n\n".join(parts)


def build_summary_request(
    folded: Sequence[Message], policy: CompactionPolicy
) -> LLMRequest:
    """构造摘要请求：一次普通的模型调用（没有工具、没有流式）。"""
    instruction = SUMMARY_INSTRUCTION.format(
        limit=policy.summary_token_limit,
        chars=int(policy.summary_token_limit / 0.6),  # 中文 0.6 token/字 反推
    )
    transcript = build_transcript(
        folded,
        per_message_chars=policy.per_message_chars,
        total_chars=policy.transcript_char_budget,
    )
    return LLMRequest(
        messages=[
            system(instruction),
            user(f"【需要压缩的对话历史】\n{transcript}\n\n{SUMMARY_TAIL}"),
        ]
    )


class Compactor:
    """把"什么时候压、压哪一段、怎么摘要"打包成一个可调用对象。

    只依赖 LLMProvider 协议（摘要本身是一次普通模型调用）；**不认识 AgentLoop**，
    测试可以直接喂消息列表。调用方（循环）负责把结果落到内存历史与会话日志。
    """

    def __init__(self, provider: LLMProvider, policy: CompactionPolicy) -> None:
        self._provider = provider
        self._policy = policy

    @property
    def policy(self) -> CompactionPolicy:
        return self._policy

    def maybe_compact(
        self,
        messages: Sequence[Message],
        tools: Iterable[ToolSpec],
        *,
        measured: int | None = None,
        protect_from: int | None = None,
        manual: bool = False,
    ) -> CompactionOutcome | None:
        """超阈值（或 `manual=True`）就压缩并返回结果；否则返回 None。

        异常**不吞**：摘要调用失败、摘要为空都往上抛，由调用方决定怎么处理
        （自动路径的选择是"跳过本次压缩、照常继续"，即 fail-soft；
        手动路径是用户按下的动作，失败要让人看见）。
        """
        plan = plan_compaction(
            messages, tools, self._policy,
            measured=measured, protect_from=protect_from, manual=manual,
        )
        if plan is None:
            return None

        response = self._provider.complete(
            build_summary_request(messages[plan.start:plan.cut], self._policy)
        )
        text = (response.message.content or "").strip()
        if not text:
            raise ValueError("摘要调用返回了空文本（压缩放弃）")

        summary_message = user(compose_summary(text))
        summary_tokens = message_tokens(summary_message)
        return CompactionOutcome(
            start=plan.start,
            cut=plan.cut,
            folded=plan.folded,
            summary=text,
            before_tokens=plan.used,
            after_tokens=int(round(plan.kept_tokens + summary_tokens)),
            summary_tokens=int(round(summary_tokens)),
            over_limit=summary_tokens > self._policy.summary_token_limit,
            trigger="manual" if manual else "auto",
        )


__all__ = [
    "DEFAULT_KEEP_RATIO",
    "DEFAULT_SUMMARY_RATIO",
    "DEFAULT_TRIGGER_RATIO",
    "SUMMARY_HEADER",
    "SUMMARY_INSTRUCTION",
    "SUMMARY_TAIL",
    "CompactionOutcome",
    "CompactionPlan",
    "CompactionPolicy",
    "Compactor",
    "build_summary_request",
    "build_transcript",
    "choose_cut",
    "choose_manual_cut",
    "compose_summary",
    "first_conversation_index",
    "is_compaction_summary",
    "last_turn_start",
    "plan_compaction",
]
