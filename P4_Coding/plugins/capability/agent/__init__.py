"""agent 能力插件组：循环实现与模式管理。

    loop.py   agent-loop   默认驱动器（模型 → 工具 → 重复）；可替换实现
    admin.py  agent-admin  模式预设（default / quick / careful）

**为什么是 capability 而不是 base**：`agent`（注册表与句柄）是接缝，归 base；
这两个都是"填进接缝的具体实现/策略"——换一个驱动器（比如一步收尾的假循环）
产品照常起。与 `llm`（base）+ `llm-provider`（capability）同构。
"""
