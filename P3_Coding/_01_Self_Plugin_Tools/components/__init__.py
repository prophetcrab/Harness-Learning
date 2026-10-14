"""components —— 纯构件层：不依赖 kernel、不认识"插件"。

这里放的是从 P2 各阶段沉淀下来的**可复用构件**（LLM 词汇与 provider、
会话日志、提示词装配、工具管线、能力接缝的实现……）。它们本身只是普通的
Python 代码；让它们"活起来"的装配、生命周期与依赖管理，全部由 kernel +
plugins 两层负责。

分层约定（详见包 README）：

    kernel      插件内核（Context：服务 / 事件 / effect / inject）
    components  纯构件（本包；被插件引用，自己不知道插件存在）
    plugins     能力插件（把构件装成服务，经 ctx 生命周期管理）
"""
