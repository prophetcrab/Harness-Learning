"""harness.webui —— 本地可视化页面（stdlib http.server + NDJSON 事件流）。

- server.py   服务与 API：GET 只读接口 + POST send（NDJSON 流）+ fork / 模拟崩溃
- index.html  页面：三栏 = 会话列表 | 对话 | 日志轨迹（随对话实时生长）

零新增依赖（标准库实现），直接调用真实 API（读取项目根 .env 里的 key）。
"""
