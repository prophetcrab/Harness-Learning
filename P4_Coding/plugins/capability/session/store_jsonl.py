"""session-store-jsonl —— 会话持久化（能力插件）。

磁盘布局、header、读写句柄、撕裂尾巴修复都在 `components/session/jsonl.py`；
本插件负责把它们接到事件总线上：

    写：订阅 `session/event` → 该会话的写句柄（首次写入时创建）
    读：订阅 `session/load`（waterfall）→ 只读句柄读回事件
    屏障：`store.flush()` 把所有打开的写句柄 fsync 一遍
    观测：`store.stat(id)` / `store.list()` **只读 header + 文件 stat**

**一个会话一个写者**：同一会话的写句柄在插件内是唯一的；重复打开返回同一个。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core import Context

#: 事件总线上的常量（字符串；插件之间不 import，见决策 0053）。
SESSION_EVENT = "session/event"
SESSION_LOAD = "session/load"


@dataclass(frozen=True)
class SessionEvent:
    """本插件自己的事件副本（字段与 sessions 插件的同名类型一致）。

    读日志时由 `from_dict` 构造；写日志时由字典构造。**刻意不共享代码**：
    插件之间只靠"事件形状"这个约定协作。
    """

    seq: int
    kind: str
    payload: Mapping[str, Any]
    at: str
    ignorable: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "seq": self.seq,
            "kind": self.kind,
            "payload": dict(self.payload),
            "at": self.at,
        }
        if self.ignorable:
            data["ignorable"] = True
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionEvent:
        return cls(
            seq=int(data["seq"]),
            kind=str(data["kind"]),
            payload=dict(data.get("payload") or {}),
            at=str(data.get("at", "")),
            ignorable=bool(data.get("ignorable", False)),


#: 持久化服务的名字（管理面可以读它拿 last_repair / 路径）。
        )
SESSION_STORE_SERVICE = "session-store-jsonl"


class JsonlSessionStore:
    """一个根目录下的会话日志：写句柄池 + 只读查询。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.last_repair: RepairReport | None = None
        self._writers: dict[str, SessionWriter] = {}

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def path_for(self, session_id: str, *, cwd: str | Path | None = None) -> Path:
        """会话日志的路径（已有文件 → 返回已有的；否则按 cwd 推导新路径）。"""
        found = find_session_files(self.root, session_id)
        if found:
            return found[0][1]
        return session_path(self.root, session_id, cwd=cwd, version=FORMAT_VERSION)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def append(
        self, session_id: str, event: SessionEvent, *, cwd: str | Path | None = None
    ) -> None:
        """把一条事件写进该会话的写句柄（首次调用时创建）。"""
        writer = self._writers.get(session_id)
        if writer is None:
            path = self.path_for(session_id, cwd=cwd)
            header = SessionHeader(
                session_id=session_id,
                created_at=datetime.now(UTC).isoformat(),
                cwd=str(cwd) if cwd is not None else None,
            )
            writer = SessionWriter(path, header)
            self._writers[session_id] = writer
        writer.append(event)

    def flush(self) -> None:
        """全局耐久屏障：所有打开的写句柄 fsync。"""
        for writer in self._writers.values():
            writer.flush()

    def close_session(self, session_id: str) -> None:
        writer = self._writers.pop(session_id, None)
        if writer is not None:
            writer.close()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> list[SessionEvent]:
        """读回该会话的全部事件；没有日志 → 空列表。"""
        found = find_session_files(self.root, session_id)
        if not found:
            return []
        _version, path = found[0]
        events, report = SessionReader(path).events()
        self.last_repair = report
        return events

    def stat(self, session_id: str) -> dict[str, Any] | None:
        """**只读 header + 文件 stat**（不看事件行）。"""
        found = find_session_files(self.root, session_id)
        if not found:
            return None
        version, path = found[0]
        header = SessionReader(path).header()
        info = path.stat()
        return {
            "session_id": header.session_id,
            "cwd": header.cwd,
            "created_at": header.created_at,
            "version": version,
            "size_bytes": info.st_size,
            "revision": f"{info.st_size}:{int(info.st_mtime_ns)}",
            "path": str(path),
        }

    def list(self) -> list[dict[str, Any]]:
        """列出 root 下所有会话（只读 header + stat）。"""
        base = self.root
        if not base.is_dir():
            return []
        snapshots: list[dict[str, Any]] = []
        for project in sorted(base.iterdir()):
            if not project.is_dir():
                continue
            for directory in sorted(project.iterdir()):
                if not directory.is_dir():
                    continue
                candidates = [
                    item for item in directory.iterdir() if item.name.startswith("session.v")
                ]
                if not candidates:
                    continue
                latest = max(candidates, key=lambda item: item.name)
                try:
                    header = SessionReader(latest).header()
                except SessionFormatError:
                    continue
                info = latest.stat()
                snapshots.append(
                    {
                        "session_id": header.session_id,
                        "cwd": header.cwd,
                        "created_at": header.created_at,
                        "project": project.name,
                        "size_bytes": info.st_size,
                        "revision": f"{info.st_size}:{int(info.st_mtime_ns)}",
                    }
                )
        return snapshots

    def session_ids(self) -> tuple[str, ...]:
        return tuple(item["session_id"] for item in self.list())

    def snapshot(self) -> dict[str, Any]:
        sessions = self.list()
        return {
            "root": str(self.root),
            "sessions": [item["session_id"] for item in sessions],
            "details": sessions,
            "open_writers": sorted(self._writers),
            "last_repair": (
                None
                if self.last_repair is None
                else {
                    "lines_total": self.last_repair.lines_total,
                    "lines_ok": self.last_repair.lines_ok,
                    "lines_broken": self.last_repair.lines_broken,
                    "truncated_bytes": self.last_repair.truncated_bytes,
                    "ignored_unknown": self.last_repair.ignored_unknown,
                }
            ),
        }


class JsonlSessionStorePlugin:
    """提供 `session-store-jsonl` 服务，并挂上读 / 写 / 屏障。"""

    name = "session-store-jsonl"
    inject = ("sessions",)

    def __init__(self, *, root: str | Path) -> None:
        self._root = root

    def setup(self, ctx: Context):
        store = JsonlSessionStore(self._root)
        ctx.provide(SESSION_STORE_SERVICE, store)

        def on_event(session_id: str, session_event: Any, **_: Any) -> None:
            # 广播过来的是字典（sessions 插件不 import 本模块的类型）。
            event = (
                session_event
                if isinstance(session_event, SessionEvent)
                else SessionEvent.from_dict(session_event)
            )
            workspace = ctx.get("workspace")
            cwd = str(workspace.root) if workspace is not None else None
            store.append(session_id, event, cwd=cwd)
            # 耐久点：一轮结束时落盘（turn/end / session/start）。
            # 中间事件只进缓冲——逐条 fsync 是明显的 I/O 放大；崩溃最多丢
            # 当前这一轮的尾部，已经结束的轮次都在盘上。
            if event.kind in ("turn/end", "session/start"):
                store.flush()

        def on_load(current: list[dict[str, Any]] | None, *, session_id: str, **_: Any):
            # 返回**字典**：消费方（sessions）自己去构造它的事件类型。
            events = [event.to_dict() for event in store.load(session_id)]
            return events or current

        ctx.on(SESSION_EVENT, on_event)
        ctx.on(SESSION_LOAD, on_load)

        def dispose() -> None:
            # 卸载 = 关掉所有写句柄（先 fsync 再关，不丢最后一批）。
            for session_id in list(store._writers):
                store.close_session(session_id)

        return dispose


def session_store_jsonl_plugin(**config: Any) -> JsonlSessionStorePlugin:
    """工厂：装配单的 `config` 里给 `root`（会话目录）。"""
    return JsonlSessionStorePlugin(**config)


__all__ = [
    "FORMAT_MARK",
    "FORMAT_VERSION",
    "JsonlSessionStore",
    "JsonlSessionStorePlugin",
    "RepairReport",
    "SESSION_STORE_SERVICE",
    "SessionFormatError",
    "SessionHeader",
    "SessionReader",
    "SessionWriter",
    "encode_session_id",
    "find_session_files",
    "last_complete_offset",
    "project_dir_name",
    "session_path",
    "session_store_jsonl_plugin",
    "truncate_torn_tail",
]


# ======================================================================
# 以下合并自 components/session/jsonl.py
# ======================================================================


#: 本版本认识的事件类型（与 sessions 插件的白名单保持一致；两边各存一份）。
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session/start",
        "system/message",
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "assistant",
        "tool/call",
        "tool/result",
        "compact/checkpoint",
        "user/message",
    }

#: 格式标记与版本（写进首行）。
)
FORMAT_MARK = "p4-session"
FORMAT_VERSION = 1

_VERSION_RE = re.compile(r"^session\.v(\d+)\.jsonl$")
#: 项目目录名里不允许的字符（保留可读性）。
_PROJECT_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class SessionFormatError(ValueError):
    """日志文件的 header / 结构不合法（拒绝解读整份文件）。"""


@dataclass(frozen=True)
class SessionHeader:
    """首行的会话元信息（只读，创建后不变）。"""

    session_id: str
    created_at: str
    cwd: str | None = None
    version: int = FORMAT_VERSION

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "format": FORMAT_MARK,
            "version": self.version,
            "session_id": self.session_id,
            "created_at": self.created_at,
        }
        if self.cwd:
            data["cwd"] = self.cwd
        return data

    @classmethod
    def from_dict(cls, data: object) -> SessionHeader:
        if not isinstance(data, dict):
            raise SessionFormatError(f"header 必须是字典：{data!r}")
        if data.get("format") != FORMAT_MARK:
            raise SessionFormatError(
                f"不是本项目的会话日志（format={data.get('format')!r}）"
            )
        version = data.get("version")
        if version != FORMAT_VERSION:
            raise SessionFormatError(
                f"不支持的会话格式版本：{version!r}（本版本只读 v{FORMAT_VERSION}）"
            )
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise SessionFormatError(f"header 缺少 session_id：{data!r}")
        return cls(
            session_id=session_id,
            created_at=str(data.get("created_at", "")),
            cwd=(str(data["cwd"]) if data.get("cwd") else None),
            version=FORMAT_VERSION,


# ---------------------------------------------------------------------------
# 路径派生
# ---------------------------------------------------------------------------


        )
def encode_session_id(session_id: str) -> str:
    """会话 id → 安全路径段（单射：不同 id 必然不同名，且无路径穿越）。"""
    return quote(session_id, safe="")


def project_dir_name(cwd: str | Path | None) -> str:
    """规范化 cwd → 可读的项目目录名（相同的 cwd 串共享一个目录）。"""
    if cwd is None:
        return "_no-cwd_"
    # 先把两种分隔符都归一成 `/`，再把路径段折成 `_`。
    normalized = str(cwd).replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    safe = "_".join(_PROJECT_UNSAFE.sub("_", part) for part in parts)
    return f"--{safe or 'root'}--"


def session_path(
    root: str | Path,
    session_id: str,
    *,
    cwd: str | Path | None = None,
    version: int = FORMAT_VERSION,
) -> Path:
    """一个会话的日志文件路径（新会话用）。"""
    return (
        Path(root)
        / project_dir_name(cwd)
        / encode_session_id(session_id)
        / f"session.v{version}.jsonl"


    )
def find_session_files(root: str | Path, session_id: str) -> list[tuple[int, Path]]:
    """在 root 下找某个会话的所有版本文件，按版本降序返回。"""
    base = Path(root)
    if not base.is_dir():
        return []
    encoded = encode_session_id(session_id)
    found: list[tuple[int, Path]] = []
    for project in base.iterdir():
        if not project.is_dir():
            continue
        directory = project / encoded
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            match = _VERSION_RE.match(item.name)
            if match:
                found.append((int(match.group(1)), item))
    return sorted(found, key=lambda pair: pair[0], reverse=True)


# ---------------------------------------------------------------------------
# 撕裂尾巴
# ---------------------------------------------------------------------------


def last_complete_offset(path: str | Path) -> int:
    """文件里最后一个换行之后的位置（即"完整行"的字节长度）。"""
    with Path(path).open("rb") as handle:
        data = handle.read()
    index = data.rfind(b"\n")
    return index + 1 if index >= 0 else 0


def truncate_torn_tail(path: str | Path) -> int:
    """截掉不完整的末行；返回被截掉的字节数（0 = 没有坏尾巴）。"""
    target = Path(path)
    if not target.exists():
        return 0
    size = target.stat().st_size
    valid = last_complete_offset(target)
    if valid >= size:
        return 0
    with target.open("r+b") as handle:
        handle.truncate(valid)
        handle.flush()
        os.fsync(handle.fileno())
    return size - valid


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairReport:
    """一次读取的修复报告。"""

    lines_total: int = 0
    lines_ok: int = 0
    lines_broken: int = 0
    #: 被丢弃的不完整末行字节数（只读打开时只报告，不写盘）。
    truncated_bytes: int = 0
    #: 带 `ignorable` 标记、被跳过的未知事件数。
    ignored_unknown: int = 0

    @property
    def repaired(self) -> bool:
        return self.lines_broken > 0 or self.truncated_bytes > 0


def _read_lines(path: Path) -> tuple[SessionHeader, list[tuple[int, str]], int]:
    """读 header + 事件行（原文）；返回 (header, [(行号, 文本)], 坏尾巴字节数)。"""
    with path.open("rb") as handle:
        raw = handle.read()
    valid = raw.rfind(b"\n") + 1
    torn = len(raw) - valid
    complete = raw[:valid]
    lines = complete.decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise SessionFormatError(f"空日志文件：{path}")
    try:
        header = SessionHeader.from_dict(json.loads(lines[0]))
    except (json.JSONDecodeError, SessionFormatError) as exc:
        raise SessionFormatError(f"无法解析 header（{path}）：{exc}") from exc
    return header, [(number, text) for number, text in enumerate(lines[1:], start=1)], torn


# ---------------------------------------------------------------------------
# 读句柄
# ---------------------------------------------------------------------------


class SessionReader:
    """只读句柄：读 header / 读全部事件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def header(self) -> SessionHeader:
        header, _lines, _torn = _read_lines(self.path)
        return header

    def events(self) -> tuple[list[SessionEvent], RepairReport]:
        """读回全部事件；只有**最后一行**允许是撕裂的坏行，中间坏行直接报错。"""
        header, lines, torn = _read_lines(self.path)
        events: list[SessionEvent] = []
        broken = 0
        ignored = 0
        total = len(lines)
        for index, (number, text) in enumerate(lines):
            is_last = index == len(lines) - 1
            stripped = text.strip()
            if not stripped:
                if is_last:
                    broken += 1
                    continue
                raise SessionFormatError(f"{self.path} 第 {number + 1} 行是空行")
            try:
                event = SessionEvent.from_dict(json.loads(stripped))
            except Exception as exc:  # noqa: BLE001 —— 分类后决定跳过还是拒绝
                if is_last:
                    broken += 1  # 撕裂的末行：丢弃
                    continue
                raise SessionFormatError(
                    f"{self.path} 第 {number + 1} 行损坏：{exc}"
                ) from exc
            # `from_dict` 只对"未知且无标记"抛错；带标记的未知事件会走到这里。
            if event.kind not in KNOWN_EVENT_TYPES:
                ignored += 1
                continue
            events.append(event)
        return events, RepairReport(
            lines_total=total,
            lines_ok=len(events),
            lines_broken=broken,
            truncated_bytes=torn,
            ignored_unknown=ignored,

        )
    def close(self) -> None:
        """只读句柄没有资源要关（保持接口对称）。"""


# ---------------------------------------------------------------------------
# 写句柄
# ---------------------------------------------------------------------------


class SessionWriter:
    """写句柄：打开即占位（写 header）、修复撕裂尾巴、逐条追加。

    **一个会话一个写者**：同一个进程内由 store 保证；跨进程的写者认领没有
    实现（需要文件锁，见决策记录）。
    """

    def __init__(
        self,
        path: str | Path,
        header: SessionHeader,
        *,
        repair: bool = True,
    ) -> None:
        self.path = Path(path)
        self.header_value = header
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repaired_bytes = 0
        if self.path.exists() and self.path.stat().st_size > 0:
            if repair:
                self.repaired_bytes = truncate_torn_tail(self.path)
            existing = SessionReader(self.path).header()
            if existing.session_id != header.session_id:
                raise SessionFormatError(
                    f"会话 id 不匹配：文件里是 {existing.session_id!r}，"
                    f"打开的是 {header.session_id!r}"
                )
            self._handle = self.path.open("a", encoding="utf-8")
        else:
            self._handle = self.path.open("a", encoding="utf-8")
            self._write_line(header.to_dict())
            # header 立刻落盘：`stat` / `list` 只读 header，不能看到"空文件"。
            self.flush()

    def _write_line(self, data: dict[str, object]) -> None:
        self._handle.write(json.dumps(data, ensure_ascii=False) + "\n")

    def append(self, event: SessionEvent) -> None:
        """追加一条事件（写入缓冲，不立即 fsync——耐久屏障是 flush）。"""
        self._write_line(event.to_dict())

    def flush(self) -> None:
        """耐久屏障：刷新缓冲并 fsync。"""
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self.flush()
            self._handle.close()

    def __enter__(self) -> SessionWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
