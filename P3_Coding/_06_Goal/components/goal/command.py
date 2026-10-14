"""goal 命令 —— `/goal` 的人机接口（不花模型的钱）。

**为什么单开一个模块**：`/goal` 要在两个入口用（REPL 与网页命令菜单），
而"命令怎么解析、结果怎么排版成给人看的一段字"是纯逻辑，与入口无关。
两个入口各写一遍解析必然漂移（一个支持 `clear` 另一个忘了），所以收在这里。

用法：

    /goal                     看一眼当前目标
    /goal <目标内容>            直接新建（**最常用，不用打 new**）
    /goal new <目标内容>        同上；可带 --rounds N 指定轮数上限
    /goal edit <新内容>         改目标内容
    /goal pause | resume      暂停 / 恢复
    /goal done                标记完成
    /goal block <原因>         标记被卡住
    /goal clear               清掉

**一个逗号的区别**：`/goal 写个飞机大战` 会被当成"新建一个叫「写个飞机大战」的目标"，
而 `/goal pause` 是执行暂停——先看第一个词是不是子命令，是就执行，不是就当目标内容。
这样最符合直觉（想说目标就直接说），代价是要防"子命令打错一个字母"被静默当成
目标内容，所以配了个拼写纠错：短英文词只差一点时提示"想用 pause 吗？"。

返回值是**给人读的文本**（入口直接打印/显示），不抛异常——命令打错了
应该回一句"怎么用"，而不是把界面搞出个 traceback。
"""

from __future__ import annotations

from dataclasses import dataclass

# 从子模块直接导入（不从包 __init__ 导：那会与包内再导出形成循环）
from components.goal.domain import DEFAULT_MAX_GOAL_ROUNDS, GoalError, GoalProjection
from components.goal.store import GoalStore

USAGE = (
    "用法：/goal [<目标内容> | new <目标内容> [--rounds N] | edit <新内容> | "
    "pause | resume | done | block <原因> | clear]\n"
    "  不带参数 = 看当前目标；直接写内容 = 新建一个目标。"
)

#: 中文别名 → 规范子命令（人两种都爱打）。
_ALIASES = {
    "new": "new", "建": "new", "新建": "new",
    "edit": "edit", "改": "edit", "修改": "edit",
    "pause": "pause", "暂停": "pause",
    "resume": "resume", "继续": "resume", "恢复": "resume",
    "done": "done", "complete": "done", "完成": "done",
    "block": "block", "卡住": "block", "阻塞": "block",
    "clear": "clear", "清": "clear", "清掉": "clear", "删除": "clear",
}


def closest_subcommand(token: str) -> str | None:
    """这个词像不像"打错的子命令"？像则返回最接近的那个。

    只对**短英文词**判断：真实的目标内容多半是中英混排的长句（如
    `写一个飞机大战html`），不会被误判成拼写错误。反过来，`/goal paus`
    这种一个字母的笔误必须拦下——否则它会静默建一个叫 "paus" 的目标。
    """
    if not token.isascii() or not token.isalpha() or len(token) > 12:
        return None
    lowered = token.lower()
    for name in _ALIASES:
        if not name.isascii():
            continue
        if name.startswith(lowered) or lowered.startswith(name):
            return name
    return None


@dataclass(frozen=True)
class GoalCommandResult:
    """一次 `/goal` 的结果：一段给人看的文本 + 是否改了状态。"""

    text: str
    changed: bool = False


def format_goal(goal: GoalProjection | None) -> str:
    """把当前目标排成人看的一小段（无目标时也给一句话，而不是空白）。"""
    if goal is None:
        return "当前没有目标。用 `/goal new <目标内容>` 建一个。"
    phase_cn = {
        "active": "进行中", "paused": "已暂停", "blocked": "被卡住", "complete": "已完成",
    }.get(goal.phase, goal.phase)
    lines = [
        f"目标（{phase_cn}，revision {goal.revision}）：{goal.objective}",
        f"  续跑轮次：{goal.rounds_started} / {goal.max_goal_rounds}",
    ]
    if goal.goal.blocked_reason is not None:
        reason = goal.goal.blocked_reason
        lines.append(f"  被卡原因：{reason.message}（{reason.code}）")
    lines.append(f"  id：{goal.id}")
    return "\n".join(lines)


def _parse_rounds(args: list[str]) -> tuple[list[str], int | None, str | None]:
    """从参数里摘出 `--rounds N`；返回 (剩余参数, N, 错误)。"""
    if "--rounds" not in args:
        return args, None, None
    index = args.index("--rounds")
    if index + 1 >= len(args):
        return args, None, "--rounds 后面要跟一个数字"
    try:
        value = int(args[index + 1])
    except ValueError:
        return args, None, f"--rounds 要给数字，收到 {args[index + 1]!r}"
    if value < 1:
        return args, None, f"--rounds 必须 >= 1，收到 {value}"
    return args[:index] + args[index + 2 :], value, None


def _create_from_text(store: GoalStore, args: list[str]) -> GoalCommandResult:
    """`/goal <内容>` 直接建目标（**不用先打 new**，这是最常用的一条路）。

    先看第一个词像不像打错的子命令：像就提示纠正（`/goal paus` 不该静默建出
    一个叫 "paus" 的目标）；不像就把整行当目标内容。
    """
    typo = closest_subcommand(args[0])
    if typo is not None:
        return GoalCommandResult(f"不认识子命令：{args[0]}——想用 {typo} 吗？\n{USAGE}")
    words, rounds, error = _parse_rounds(args)
    if error is not None:
        return GoalCommandResult(error)
    objective = " ".join(words).strip()
    if not objective:
        return GoalCommandResult(f"--rounds 还要配目标内容。\n{USAGE}")
    try:
        store.create(objective, max_goal_rounds=rounds)
    except GoalError as exc:
        return GoalCommandResult(f"操作被拒绝：{exc}")
    return GoalCommandResult(
        f"已建立目标（轮数上限 {rounds or DEFAULT_MAX_GOAL_ROUNDS}）：{objective}\n"
        "接下来发一条消息就会开始动手；没做完之前它会自动续跑。",
        changed=True,
    )


def run_goal_command(store: GoalStore, line: str) -> GoalCommandResult:
    """执行一条 `/goal ...`；`line` 是去掉 `/goal` 之后的剩余文本。

    设计成"**永不抛异常**"：人打错命令要看到用法提示，而不是界面报错。
    真正的失败（目标不存在、版本对不上、非法流转）都变成一句人话回过去。
    """
    args = [piece for piece in line.strip().split() if piece]
    if not args:
        return GoalCommandResult(format_goal(store.current()))

    raw_action, rest = args[0], args[1:]
    action = _ALIASES.get(raw_action.lower())
    if action is None:
        # 不是子命令 → 当成目标内容（`/goal 写个飞机大战` 直接建目标）
        return _create_from_text(store, args)

    rest, rounds, error = _parse_rounds(rest)
    if error is not None:
        return GoalCommandResult(error)

    try:
        if action == "new":
            objective = " ".join(rest).strip()
            if not objective:
                return GoalCommandResult(f"new 需要目标内容。\n{USAGE}")
            store.create(objective, max_goal_rounds=rounds)
            return GoalCommandResult(
                f"已建立目标（轮数上限 {rounds or DEFAULT_MAX_GOAL_ROUNDS}）：{objective}\n"
                "接下来发一条消息就会开始动手；没做完之前它会自动续跑。",
                changed=True,
            )
        if action == "edit":
            objective = " ".join(rest).strip()
            if not objective and rounds is None:
                return GoalCommandResult(f"edit 需要新内容（或 --rounds N）。\n{USAGE}")
            store.edit(objective=objective or None, max_goal_rounds=rounds)
            return GoalCommandResult("已更新目标。\n" + format_goal(store.current()), changed=True)
        if action == "pause":
            store.pause()
            return GoalCommandResult("已暂停（不再自动续跑）。\n" + format_goal(store.current()),
                                     changed=True)
        if action == "resume":
            store.resume()
            return GoalCommandResult("已恢复。\n" + format_goal(store.current()), changed=True)
        if action == "done":
            store.complete()
            return GoalCommandResult("已标记完成。\n" + format_goal(store.current()), changed=True)
        if action == "block":
            message = " ".join(rest).strip() or "未说明原因"
            store.block(code="human", message=message)
            return GoalCommandResult("已标记被卡住。\n" + format_goal(store.current()), changed=True)
        if action == "clear":
            store.clear()
            return GoalCommandResult("已清掉目标。", changed=True)
    except GoalError as exc:
        return GoalCommandResult(f"操作被拒绝：{exc}")

    return GoalCommandResult(f"未实现的子命令：{action}\n{USAGE}")   # pragma: no cover


__all__ = [
    "USAGE",
    "GoalCommandResult",
    "closest_subcommand",
    "format_goal",
    "run_goal_command",
]
