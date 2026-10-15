"""本机端口的选择 —— "要一个端口，但绝不静默抢别人的"（`_06` 实测坑）。

**坑的形状**：账本类插件（`token-usage` 8767 / `context-meter` 8768）用固定
端口，是为了给人一个稳定入口。但**同时跑两个 harness** 很常见（一个页面在
8795、另一个在 8070），两个进程都会去 bind 同一个端口：

    Linux   → 第二个 bind 抛 EADDRINUSE，插件装载失败（吵，但看得见）
    Windows → Python 的 HTTPServer 默认 `allow_reuse_address = 1`，
              **第二个 bind 不报错**——两个 socket 同时处于监听态

于是两个 harness 各自以为"我的账本在 8767"，而客户端连到哪一个由内核决定。
实测症状：页面顶栏的「Token / 上下文」徽标读的是**另一个进程**的账本——
用户在这边聊了半天，数字纹丝不动（"不随着使用更新了"）。

**做法**：bind 之前先探一下这个端口有没有主（一次本机 connect，几十微秒）。
有主就交回 `0`（让系统分配），并**如实公布实际端口**（两个插件都用
`server.server_address[1]` 填服务地址，所以 `port=0` 不会骗人）：
每个进程的页面都读自己的账本；第一个进程仍然守着固定端口当"人记的那个入口"。

**为什么不直接都改用随机端口**：固定端口的价值是"人记得住、可以直接在浏览器
敲"。保留它给第一个进程，只是拒绝**静默争抢**。
"""

from __future__ import annotations

import socket


def pick_port(host: str, port: int) -> int:
    """要一个端口：`port` 没主就用它；有主（或本来就是 0）交回 0 让系统分配。

    探测用 `connect_ex`：连得上 = 有人在听（返回 0），连不上（非 0）= 端口空着。
    `ECONNREFUSED` 与"超时"都算空着——后者在本机几乎不会出现（不经过网络栈的
    回环连接要么立刻成功要么立刻被拒）。
    """
    if port == 0:
        return 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex((host, port)) != 0:
            return port          # 没人应：端口归我
    return 0                     # 有主了：交给系统分配，别去争


__all__ = ["pick_port"]
