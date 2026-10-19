"""approval 能力插件组：审批通道。

    terminal.py  approval-terminal  终端 y/n（非 TTY 时 fail-closed）

审批服务 `approval` 由工具管线与文件策略共用（同一个 `ask(request)` 协议），
所以换一个通道（网页卡片、无人值守）不改任何消费方。
"""
