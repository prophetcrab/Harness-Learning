"""server —— M7 服务化（阶段 `_10` 新增的顶层模块）：harness 成为可连接的常驻服务。

**位置说明（P2 组织约定）**：本包与 `harness/` 平级；`harness/` 冻结为 M1–M3
基线库，本阶段一行未改。本阶段机制一句话：

    换行 JSON-RPC 2.0：客户端连上来握手（initialize），然后发 session.prompt
    跑 turn；会话按 id 缓存、日志照常落盘——harness 从"一个程序"变成"一个服务"。

组件（各层职责单向依赖）：

- `protocol.py`  帧词汇：一行 ↔ Request/Notification；稳定错误码；帧构造（golden 就钉在这）
- `service.py`   HarnessService：initialize 握手（含能力清单）+ session.prompt
                 （会话缓存 / 落盘走 `_01`–`_09` 的现成装配）+ fail-closed 默认审批
- `transport.py` LineTransport：换行收发循环；异常 → 稳定错误帧；stdio 与 StringIO 同路径

分层原则：服务不碰字节流（只认方法名 + 参数字典），传输不碰业务（只认 dispatch）——
于是整条链路可以在测试里用 StringIO 驱动，与真实 stdio 完全同构。

入口：阶段目录顶层的 `serve.py`（stdio 或 `--listen` TCP 服务；banner 写 stderr）
与 `attach.py`（跟随客户端：流式渲染事件、断线重连按 `from_seq` 补齐）。

依赖方向：`server → context/kernel/providers/harness`（装配与服务化）+ 自身三层。
"""

from server.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    PARSE_ERROR,
    Frame,
    Notification,
    Request,
    RpcError,
    error_frame,
    notification_frame,
    parse_line,
    result_frame,
)
from server.service import (
    EVENT_NOTIFICATION,
    METHODS,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    HarnessService,
    Sender,
    Subscription,
)
from server.tcp import TcpServer, make_server
from server.transport import Dispatcher, LineTransport

__all__ = [
    # 协议
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "NOT_INITIALIZED",
    "RpcError",
    "Frame",
    "Request",
    "Notification",
    "parse_line",
    "result_frame",
    "error_frame",
    "notification_frame",
    # 服务
    "HarnessService",
    "EVENT_NOTIFICATION",
    "Sender",
    "Subscription",
    "METHODS",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    # 传输
    "LineTransport",
    "Dispatcher",
    "TcpServer",
    "make_server",
]
