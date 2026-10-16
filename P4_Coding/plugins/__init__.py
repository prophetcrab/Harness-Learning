"""plugins —— 一切插件（loader 起：装配 / 树 / 热插拔；其后基础 / 应用化 / 能力）。

按决策 0042，P4 里「插件」的边界是：**harness 的一切能力都在树上**。

目前只有 `loader`（M1 步 5 起步——条目 / 装配单 / 按单装配）；
基础插件（llm / prompt / sessions / tools / agent-loop / agent）在 M2，
能力插件在 M3，应用化插件（web / cli / sdk）在 M4。
"""
