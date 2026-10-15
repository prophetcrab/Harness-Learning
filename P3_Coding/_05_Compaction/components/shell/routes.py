"""路由自述 —— 插件声明自己的 HTTP 端点（壳负责挂载与传输）。

**解决的问题**：插件的能力如果需要一条专属接口，原先**只能改壳**。`webui.py`
里写死了两条：

    POST /api/sessions/<id>/compact     ← 压缩插件的（10 处引用）
    GET/POST /api/approvals[/<id>]      ← 审批插件的

于是"加一个需要端点的插件"要动壳。本模块把端点变成插件的**声明**：

    routes = (RouteSpec("POST", "/api/sessions/<id>/compact", handler),)

壳遍历所有插件的声明建路由表，命中就调 handler。

**三条纪律**：

1. **插件不碰 HTTP**。handler 收到的是 `RouteRequest`——里面是**已经解析好的**
   参数与 JSON 体；要回什么用 `request.json(...)` 或 `request.stream()`。
   分块传输、CRLF 分帧、Content-Type 这些壳负责（它们最容易写错，实测踩过
   三次：HTTP/1.0 退化、漏写终止块、双重编码）。
2. **`<名字>` 捕获路径段**（单段，不含 `/`），壳负责 URL 解码。
3. **同 method + 同 pattern 撞车 → 装载期就报错**（fail loud），不静默覆盖。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote

#: 允许的 HTTP 方法（本练习只需要这两个）。
METHODS: tuple[str, ...] = ("GET", "POST")


class FrameWriter(Protocol):
    """流式响应的写端（壳实现，插件只用这两个方法）。

    `frame` 写一帧（NDJSON 一行）；`close` 收尾。**无论 handler 怎么退出，
    壳都会保证收尾**——插件的 handler 里用 try/finally 更稳妥，但不写也不会
    让客户端挂住（壳兜底，这是踩过坑之后加的保障）。
    """

    def frame(self, kind: str, payload: dict[str, Any]) -> None:
        ...

    def close(self) -> None:
        ...


class RouteResponder(Protocol):
    """壳提供给 handler 的"怎么回"（HttpHandler 实现它）。"""

    def json(self, status: int, payload: Any) -> None:
        ...

    def stream(self) -> FrameWriter:
        ...


@dataclass
class RouteRequest:
    """一次插件路由请求：路径参数 + JSON 体 + 回应方式。

    `ctx` 是内核上下文——handler 通常用它现取需要的服务（不要抓存，见
    webui 模块里那条"服务一律现取"的纪律）。
    """

    ctx: Any
    params: dict[str, str]
    body: dict[str, Any]
    responder: RouteResponder

    def json(self, payload: Any, status: int = 200) -> None:
        """回一个 JSON 响应。"""
        self.responder.json(status, payload)

    def stream(self) -> FrameWriter:
        """开始一个流式（NDJSON）响应，返回帧写入端。"""
        return self.responder.stream()


@dataclass(frozen=True)
class RouteSpec:
    """一个插件端点：方法 + 路径模式 + 处理器。

    `handler(request: RouteRequest) -> None`——不返回值，怎么回由 request 决定。
    """

    method: str
    pattern: str
    handler: Callable[[RouteRequest], None]
    #: 排序权重（决定匹配优先级；相同则按"先声明者优先"的稳定顺序）。
    order: int = 100

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in METHODS:
            raise ValueError(f"不支持的 HTTP 方法：{self.method}（可用：{METHODS}）")
        object.__setattr__(self, "method", method)
        if not self.pattern.startswith("/"):
            raise ValueError(f"路径模式必须以 / 开头：{self.pattern!r}")
        if not callable(self.handler):
            raise ValueError(f"handler 必须可调用：{self.handler!r}")

    def to_dict(self) -> dict[str, Any]:
        """给页面看的形态（**不含 handler**——那不可序列化）。"""
        return {"method": self.method, "pattern": self.pattern, "order": self.order}


def match_path(pattern: str, path: str) -> dict[str, str] | None:
    """按段匹配；`<名字>` 捕获一段（URL 解码后返回）。不匹配返回 None。

    刻意不做正则、不做通配：插件的端点模式应当简单到一眼看懂。
    """
    wanted = [seg for seg in pattern.strip("/").split("/") if seg]
    got = [seg for seg in path.strip("/").split("/") if seg]
    if len(wanted) != len(got):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(wanted, got, strict=True):
        if expected.startswith("<") and expected.endswith(">"):
            name = expected[1:-1]
            if not name:
                return None
            params[name] = unquote(actual)
        elif expected != actual:
            return None
    return params


@dataclass
class RouteTable:
    """从若干插件声明汇总出的路由表（壳用它分派）。

    `dispatch` 找到就执行并返回 True；找不到返回 False（由壳决定 404）。
    重复（同 method + 同 pattern）在 `build` 时就抛错——静默覆盖会让一个插件
    悄悄顶掉另一个插件的端点。
    """

    routes: list[RouteSpec] = field(default_factory=list)

    @classmethod
    def build(cls, specs: Iterable[RouteSpec]) -> RouteTable:
        ordered = sorted(specs, key=lambda r: (r.order, r.method, r.pattern))
        seen: dict[tuple[str, str], RouteSpec] = {}
        for route in ordered:
            key = (route.method, route.pattern)
            if key in seen:
                raise ValueError(
                    f"路由撞车：{route.method} {route.pattern} 被声明了两次"
                    "（两个插件用了同一个端点？）"
                )
            seen[key] = route
        return cls(routes=list(seen.values()))

    def find(self, method: str, path: str) -> tuple[RouteSpec, dict[str, str]] | None:
        for route in self.routes:
            if route.method != method.upper():
                continue
            params = match_path(route.pattern, path)
            if params is not None:
                return route, params
        return None

    def dispatch(
        self,
        method: str,
        path: str,
        *,
        ctx: Any,
        body: dict[str, Any],
        responder: RouteResponder,
    ) -> bool:
        """命中就执行并返回 True；未命中返回 False。"""
        hit = self.find(method, path)
        if hit is None:
            return False
        route, params = hit
        route.handler(
            RouteRequest(ctx=ctx, params=params, body=body, responder=responder)
        )
        return True

    def __len__(self) -> int:
        return len(self.routes)


__all__ = [
    "METHODS",
    "FrameWriter",
    "RouteRequest",
    "RouteResponder",
    "RouteSpec",
    "RouteTable",
    "match_path",
]
