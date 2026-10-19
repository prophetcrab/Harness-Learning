"""manifests —— 装配单（产品形态就是数据）。

三份单子，同一套条目形状：

    minimal.py    离线剧本（Python 字面量；transport 是函数，只有代码能表达）
    realtime.py   真实模型（在 minimal 上换掉 llm-provider 那一行）
    minimal.yaml  YAML 版（两条路由都装好，`--provider` 切换；改形态不改代码）

YAML 的读取与校验在 `load.py`：只认数据，不 import 插件；插件白名单仍由
`minimal.catalog()` 提供。M4 会加 `web` / `cli` / `sdk` 三份——它们的差别
只在"应用化插件"那一行。
"""
