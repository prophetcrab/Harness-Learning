"""_04_Session_Log 的 CLI：会话列表 / 查看 / 继续对话 / 分叉。

用法（在 _04_Session_Log 目录下）：

    python cli.py list
    python cli.py show <session_id>
    python cli.py run  <session_id> "问题" [--fake]
    python cli.py fork <src_id> <new_id> [--upto N]

约定与 demo 相同：`run` 对不存在的 id 是"创建新会话"，对已存在的 id 是"恢复"——
两者是同一条路径。默认把日志写在 ./sessions/ 下（可用 --root 改）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from llm_seam import FakeLLM, build_default_toolbox, text_reply
from runner import Runner, fork_session, open_session
from session import JsonlStore

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = Path(__file__).resolve().parent / "sessions"
SYSTEM_PROMPT = "你是一名严谨的中文助手，可以调用 calculate 工具做算术。"


def _build_provider(use_fake: bool):
    if use_fake:
        return FakeLLM([text_reply("（离线演示回答：已记录到会话日志）")]), "FakeLLM"
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("未找到 DEEPSEEK_API_KEY；离线体验请加 --fake（不需要 key）。")
    from llm_seam.deepseek import DeepSeekProvider

    provider = DeepSeekProvider(
        api_key=api_key,
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    return provider, "DeepSeekProvider"


def _parse_root(args: list[str]) -> tuple[Path, list[str]]:
    if "--root" in args:
        index = args.index("--root")
        path = Path(args[index + 1])
        return path, args[:index] + args[index + 2:]
    return DEFAULT_ROOT, args


def cmd_list(store: JsonlStore) -> None:
    ids = store.list_sessions()
    if not ids:
        print(f"（{store.root} 下暂无会话）")
        return
    print(f"会话（root={store.root}）：")
    for sid in ids:
        events, _ = store.load(sid)
        last = events[-1] if events else None
        turns = sum(1 for e in events if e.type == "turn/end")
        tail = f"{last.type}" if last else "空"
        print(f"  {sid}    事件 {len(events)} 条，turn {turns} 个，末事件 {tail}")


def cmd_show(store: JsonlStore, session_id: str) -> None:
    events, report = store.load(session_id)
    if report.repaired:
        print(f"[修复] {report.reason}（丢弃 {report.dropped_bytes} 字节）")
    if not events:
        print(f"会话不存在或为空：{session_id}")
        return
    print(f"会话 {session_id}（{len(events)} 条事件）")
    for event in events:
        detail = {
            k: v
            for k, v in event.data.items()
            if k in ("content", "name", "call_id", "status", "turn", "step", "final_text")
        }
        print(f"  #{event.seq:>3} {event.type:<18} {detail}")


def cmd_run(store: JsonlStore, session_id: str, question: str, use_fake: bool) -> None:
    provider, mode = _build_provider(use_fake)
    session, report = open_session(session_id, store, system_prompt=SYSTEM_PROMPT)
    if report.repaired:
        print(f"[修复] 上次会话尾部被截断，已自动修复（丢弃 {report.dropped_bytes} 字节）")

    toolbox = build_default_toolbox()
    runner = Runner(session, provider, toolbox.execute, toolbox.specs(), max_steps=6)
    print(f"provider：{mode}    会话：{session_id}    已有 turn：{session.last_turn_number}")
    result = runner.send(question)

    print(f"\n最终回答：{result.final_text}")
    print(f"本 turn：{result.turn}    status={result.status}    step 数={len(result.steps)}")
    print(f"日志已落盘：{store.path(session_id)}")


def cmd_fork(store: JsonlStore, src: str, new_id: str, upto: int | None) -> None:
    forked = fork_session(store, src, new_id, upto_seq=upto)
    print(f"已从 {src} 分叉出 {new_id}（{len(forked.events)} 条事件）")
    print(f"日志：{store.path(new_id)}")


def main(argv: list[str]) -> int:
    root, args = _parse_root(argv[1:])
    if not args:
        print(__doc__)
        return 1
    store = JsonlStore(root)
    command, rest = args[0], args[1:]
    use_fake = "--fake" in rest
    rest = [a for a in rest if a != "--fake"]

    if command == "list":
        cmd_list(store)
    elif command == "show" and rest:
        cmd_show(store, rest[0])
    elif command == "run" and len(rest) >= 2:
        cmd_run(store, rest[0], rest[1], use_fake)
    elif command == "fork" and len(rest) >= 2:
        upto = None
        if "--upto" in rest:
            index = rest.index("--upto")
            upto = int(rest[index + 1])
            rest = rest[:index]
        cmd_fork(store, rest[0], rest[1], upto)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
