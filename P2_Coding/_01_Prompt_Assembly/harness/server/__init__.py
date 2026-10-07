"""harness.server —— M7：服务化与多前端（占位，待实现）。

目标：harness 成为常驻服务；前端通过事件流跟随会话（断线可补）。

规划：
- `python -m harness serve`：先做 stdio 换行 JSON-RPC
  （initialize / session.prompt / session.follow），可选升级 HTTP + WebSocket；
- `session.follow(from_seq)` = 重放（日志）+ 订阅（实时）；
- 客户端 `python -m harness attach`：流式渲染。

验收：两个终端 serve + attach 共享同一会话；kill attach 后重连能补齐缺失事件；
协议 golden 测试。决策记录：0009-follow-equals-replay-plus-subscribe。

注：本地的可视化页面在 harness.webui（不是本包）；本包是面向多前端的 RPC 服务。
"""
