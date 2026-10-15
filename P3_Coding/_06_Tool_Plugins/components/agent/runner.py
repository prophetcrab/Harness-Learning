"""turn 执行器 —— turn 跑在**后台线程**里，HTTP 请求只是"订阅"它。

**解决什么问题**（用户提的三件事，根因都在这里）：

1. **多个会话能不能并行**：原先 webui 用一把全局锁把整个 turn 包住——A 会话跑
   30 秒，B 会话就得在门口等，哪怕它们的循环、历史、会话缓存本来互不相干。
2. **刷新页面会不会丢**：原先 turn 跑在**请求线程**里。客户端一断（刷新就是
   断连接），写帧静默失效（`_write_event` 里 `_alive=False`）——**turn 其实还在
   跑**，但页面再也看不到它，只能等它跑完再重新拉日志。

执行器把这两件事一起解决：**turn 在后台线程里跑，HTTP 请求只是订阅者**。

    POST /send      → 起一个 turn，然后订阅它的帧（和原来一样逐帧推给调用方）
    GET  /turn      → 只订阅（页面刷新后接回正在跑的那个）
    POST /interrupt → 请求取消（协作式：主循环在 step 边界与流式分块之间检查）

**订阅者是"随时可来可走"的一组**：帧到达时广播给当前全部订阅者；一个订阅者
写失败（客户端断了）不影响其他订阅者，更不影响 turn 自己跑完——这正是
"刷新页面也维持运行"的实现方式：**没人看着，turn 照跑**。

**不给后来者重放历史帧**：turn 的每一步都已经写进会话日志（recorder 逐条落盘），
页面刷新后先按日志重建（`GET /api/sessions/<id>/events` 本来就含进行中的这一段），
再接上实时帧。重放反而会与日志渲染的那份重复一遍。

**取消是协作式的**：`cancel()` 只是请求——主循环在下一次 step 边界或下一个流式
分块时看到标记就收尾（`status="cancelled"`，保留已收到的文本）。所以正在执行的
**工具调用**（比如一个 30 秒的 shell）不会被砍断，要等它自己返回。

**"这个 turn 的推流通道"怎么传给深处**：`ask` 回调会收到一个 `sink`（本 run 的
`push`），由**调用方**把它挂到它需要的地方——webui 挂给 `agent.watch`（主循环事件）
与审批策略的"按线程登记"（`attach_thread_stream`）。

> 为什么不用 ContextVar：本项目的每个阶段目录各持一份同名包，测试的 conftest
> 会驱逐 `sys.modules`，**同一个模块可能被加载成两份**。ContextVar 是模块级全局
> 对象，两份副本各有一个 → set 进去的那份和读出来的那份不是同一个，
> `get()` 永远拿到默认值（`isinstance` 已经因为这个坑栽过三次）。
> 改成"挂在 ctx 里的服务对象上"就绕开了：那些对象是**运行时单例**，
> 与模块身份无关。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 一帧的写端：`(kind, payload) -> None`。
FrameSink = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnRun:
    """一次正在跑的（或刚跑完的）turn：订阅者名单 + 终帧 + 完成信号。"""

    session_id: str
    text: str
    started_at: float
    #: 已经广播过的帧数（给"运行中"的可视化用：一眼看出它跑到第几条事件）。
    frames: int = 0
    #: 终帧（`__done` / `__error`）。跑完后才非 None——**晚到的订阅者靠它收尾**，
    #: 否则会挂在"等下一帧"上直到连接超时。
    final: tuple[str, dict[str, Any]] | None = None
    done: threading.Event = field(default_factory=threading.Event)
    _subs: list[Callable[[str, dict[str, Any]], None]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return not self.done.is_set()

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def push(self, kind: str, payload: dict[str, Any]) -> None:
        """把一帧广播给当前所有订阅者（订阅者出错只丢它自己）。

        **终帧也走这条路**（`finish` 里调）——等终帧的订阅者靠它收尾，
        所以订阅者要自己判断"这一帧是不是终帧"，别在收尾时重复发一遍
        （webui 的 `_stream_turn` 就是这么做的）。

        **锁的边界**：只在"取订阅者快照 + 记数"时持锁，不在写帧时持锁——
        写帧可能阻塞在慢客户端上，持锁会让 `unsubscribe` 一起卡住，而
        `unsubscribe` 是请求线程收尾时要调的（它会等锁）。
        """
        with self._lock:
            self.frames += 1
            subs = list(self._subs)
        for sink in subs:
            try:
                sink(kind, payload)
            except Exception:  # noqa: BLE001 —— 一个订阅者断了不影响别人
                continue

    def subscribe(self, sink: Callable[[str, dict[str, Any]], None]) -> bool:
        """订阅实时帧。返回 False = **已经跑完了**（调用方应直接发 `final`）。"""
        with self._lock:
            if self.done.is_set():
                return False
            self._subs.append(sink)
            return True

    def unsubscribe(self, sink: Callable[[str, dict[str, Any]], None]) -> None:
        """退订。

        注意 `push` 是**先取快照再写帧**的：调用本方法的瞬间可能正有一批帧
        在写给这个 sink。所以退订之后调用方收尾时仍要容忍写失败（webui 的
        `_write_event` 本来就把断线当正常情况吞掉）。
        """
        with self._lock:
            if sink in self._subs:
                self._subs.remove(sink)

    def finish(self, frame: tuple[str, dict[str, Any]]) -> None:
        """收尾：记终帧 → 广播 → 放行所有等待者。"""
        self.final = frame
        self.push(*frame)
        self.done.set()

    def summary(self) -> dict[str, Any]:
        """给界面的运行态摘要（不含帧内容）。"""
        return {
            "session": self.session_id,
            "running": self.running,
            "frames": self.frames,
            "elapsed": round(self.elapsed, 2),
        }


class TurnRunner:
    """按 session_id 管正在跑的 turn：同一条会话同时只允许一个，**不同会话并行**。

    本类不 import 任何插件/内核——只管"线程 + 订阅者"，便于单测。

        ask(session_id, text, sink) -> Any   跑一个 turn（阻塞到收尾）；sink 是
                                             这个 turn 的写帧端，调用方自行挂到
                                             需要它的地方（见模块 docstring）
        cancel(session_id) -> bool           请求取消（协作式）
        prepare(session_id) -> str | None    开场提示（如日志尾部刚被修复），可选
    """

    def __init__(
        self,
        *,
        ask: Callable[[str, str, FrameSink], Any],
        cancel: Callable[[str], bool],
        prepare: Callable[[str], str | None] | None = None,
        thread_prefix: str = "p3-turn",
    ) -> None:
        self._ask = ask
        self._cancel = cancel
        self._prepare = prepare
        self._thread_prefix = thread_prefix
        self._runs: dict[str, TurnRun] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 起 / 查
    # ------------------------------------------------------------------

    def start(self, session_id: str, text: str, *, sink: Any = None) -> TurnRun | None:
        """**保留并启动**一个 turn（`sink` 会在启动前挂上，一帧都不丢）。

        该会话已有在跑的 turn → 返回 None（调用方回 409）。

        等价于 `reserve()` + `launch()`。需要"先发响应头再启动"的调用方
        （HTTP 必须这样：先 200 再写帧，而忙时要回 409）请直接用那两个——
        见 webui 的 `_start_turn`。
        """
        run = self.reserve(session_id, text)
        if run is None:
            return None
        self.launch(run, sink=sink)
        return run

    def reserve(self, session_id: str, text: str) -> TurnRun | None:
        """**占住**这条会话（登记一个 TurnRun，但**还不起线程**）。

        为什么要把"占位"与"启动"分开：HTTP 入口必须**先发响应头**（200 +
        chunked）才可能写帧，而"会话忙"又必须回 409（那时还不能发头）。
        于是顺序是：reserve（忙就 409）→ 发头 → 挂订阅者 → launch。

        少了这一步会有一个真实的竞态：turn 线程先跑，帧都发完了订阅者才挂上，
        客户端一帧都收不到（内存里的快 turn 尤其明显——实测踩到）。
        """
        with self._lock:
            if session_id in self._runs:
                return None
            run = TurnRun(session_id=session_id, text=text, started_at=time.time())
            self._runs[session_id] = run
            return run

    def launch(self, run: TurnRun, *, sink: Any = None) -> TurnRun:
        """启动（`sink` 先挂上再起线程——保证不漏帧）。"""
        if sink is not None:
            run._subs.append(sink)   # noqa: SLF001 —— 同一模块内的协作，见 reserve
        thread = threading.Thread(
            target=self._execute,
            args=(run,),
            name=f"{self._thread_prefix}:{run.session_id}",
            daemon=True,
        )
        thread.start()
        return run

    def release(self, run: TurnRun) -> None:
        """放弃一个已 reserve 但没能启动的 run（发头失败之类的收尾）。"""
        with self._lock:
            self._runs.pop(run.session_id, None)

    def get(self, session_id: str) -> TurnRun | None:
        with self._lock:
            return self._runs.get(session_id)

    def running(self) -> dict[str, TurnRun]:
        """当前所有在跑的 turn（按 session_id）。给页面画"哪个会话在跑"。"""
        with self._lock:
            return dict(self._runs)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self, session_id: str) -> bool:
        """请求取消某会话正在跑的 turn；没有在跑就返回 False。"""
        run = self.get(session_id)
        if run is None:
            return False
        return bool(self._cancel(session_id))

    # ------------------------------------------------------------------
    # 执行（后台线程）
    # ------------------------------------------------------------------

    def _execute(self, run: TurnRun) -> None:
        try:
            if self._prepare is not None:
                note = self._prepare(run.session_id)
                if note:
                    run.push("__repair", {"message": note})
            result = self._ask(run.session_id, run.text, run.push)
            run.finish(
                (
                    "__done",
                    {
                        "turn": getattr(result, "turn", None),
                        "status": getattr(result, "status", "done"),
                        "steps": len(getattr(result, "steps", ()) or ()),
                        "final_text": getattr(result, "final_text", ""),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 异常也要以帧告知，不能静默
            run.finish(("__error", {"message": f"turn 执行失败：{exc}"}))
        finally:
            # 顺序：先 finish（置终帧 + 放行等待者）再摘表。反过来的话，
            # "刚跑完就刷新"的页面会拿到 running=True 却永远等不到终帧。
            # 摘表之后 get() 返回 None，页面看到"没有在跑"——而这一 turn 的
            # 内容早已逐条落在会话日志里，不会丢。
            with self._lock:
                self._runs.pop(run.session_id, None)


__all__ = ["FrameSink", "TurnRun", "TurnRunner"]
