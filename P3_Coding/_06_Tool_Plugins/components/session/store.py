"""JSONL 落盘 —— 事件日志的持久化层（append-only + 崩溃尾部修复）。

存储布局（对应 dsh 的 session-persistence-jsonl）：

    <root>/<session_id>/session.jsonl

每行一条事件（JSON）。写入策略是"追加 + 立即 flush"——**每条事件落盘**，而不是
攒一批再写；这样进程即便突然被杀，已确认的事件也不会丢。

**崩溃尾部修复**：如果进程在写某一行写到一半时被 kill -9，文件尾部会残留一段
不完整的 JSON（没有结尾换行）。`load()` 在读取时检测并截断这段坏尾，保证
"已确认的事件不丢、最后的半条被丢弃"。这是本练习要动手验证的核心机制。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from components.session.events import Event


@dataclass
class RepairReport:
    """一次 load 的修复报告。"""

    repaired: bool = False
    dropped_bytes: int = 0      # 被截断丢弃的字节数
    dropped_lines: int = 0      # 被丢弃的不完整行数
    reason: str = ""            # 修复原因（未修复时为空）


class JsonlStore:
    """把会话事件写成 `<root>/<session_id>/session.jsonl`。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        #: 保护 append 与 load 的"读 → 判定 → 写回截断"整体（见 load 的说明）。
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def path(self, session_id: str) -> Path:
        return self._root / session_id / "session.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).is_file()

    def append(self, session_id: str, event: Event) -> None:
        """追加一行并立即 flush（每条事件都落盘，尽量缩小崩溃丢失窗口）。"""
        target = self.path(session_id)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

    def load(self, session_id: str) -> tuple[list[Event], RepairReport]:
        """读取全部事件；自动检测并截断被截断的尾行。

        返回 (事件列表, 修复报告)。文件不存在时返回空列表。

        **`load` 不是纯读**：发现残尾时它会**写回截断**（把文件裁到最后一个完整
        行）。于是有一个真实的数据丢失窗口：

            reader: 读到 raw（尾部是半行）→ 判定 good_end
            writer: append 一条新事件（文件变长）      ← 就在这中间
            reader: 写回 raw[:good_end]                ← 刚追加的那条被裁掉了

        串行时代几乎撞不上；`_05` 收尾之后**两件事同时发生**：turn 跑在后台线程里
        持续 append，而界面每 5 秒轮询 `/api/sessions`（对每条会话调 load）——
        窗口被反复敲。整个"读 → 判定 → 写回"因此必须在一把锁里。

        代价：解析大日志时 append 要等一会儿。本项目会话日志在几百 KB 量级，
        可忽略；换来的是"日志即真相"这条地基不被并发咬穿。

        修复判据：逐行解析，记录"最后一个完整合法行"的结束位置 good_end。
        任何从 good_end 往后残留的字节（半截 JSON / 缺换行的残行）都视为崩溃
        残留，截掉并回报。若中途遇到坏行，good_end 其后的一切都丢弃（append-only
        之下，中间坏行按损坏处理，不做修补）。
        """
        target = self.path(session_id)
        # 整段持锁：读字节 → 判定 good_end → 按需写回截断。中途放锁就会让并发的
        # append 落进"被截断的那一段"里（见上面的时序图）。
        with self._lock:
            if not target.is_file():
                return [], RepairReport()
            raw = target.read_bytes()
            return self._parse_and_repair(target, raw)

    def _parse_and_repair(
        self, target: Path, raw: bytes
    ) -> tuple[list[Event], RepairReport]:
        """解析 + 按需写回截断。**调用方必须已持锁**（见 load 的说明）。"""
        events: list[Event] = []
        good_end = 0

        offset = 0
        for chunk in raw.split(b"\n"):
            line_start = offset
            offset += len(chunk) + 1  # +1 为被 split 去掉的换行符

            if not chunk:
                # 空块 = 一个正常行尾（含文件末尾的换行）；推进 good_end 越过它
                good_end = min(line_start + 1, len(raw))
                continue

            try:
                events.append(Event.from_dict(json.loads(chunk.decode("utf-8"))))
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValueError):
                # 坏行：从这一行起全部当作崩溃残留丢弃
                good_end = line_start
                break

            # 该行完整且合法：good_end 推进到本行（含换行）之后
            good_end = line_start + len(chunk) + 1

        if good_end < len(raw):
            target.write_bytes(raw[:good_end])       # 调用方已持锁
            remainder = raw[good_end:]
            return events, RepairReport(
                repaired=True,
                dropped_bytes=len(remainder),
                dropped_lines=remainder.count(b"\n") + 1,
                reason="检测到被截断的尾部（很可能上一次进程被强杀），已截断修复",
            )

        return events, RepairReport()

    def list_sessions(self) -> list[str]:
        """列出 root 下所有含 session.jsonl 的会话 id（按名字排序）。"""
        if not self._root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self._root.iterdir()
            if entry.is_dir() and (entry / "session.jsonl").is_file()
        )
