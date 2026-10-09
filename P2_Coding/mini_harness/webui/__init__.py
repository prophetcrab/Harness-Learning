"""webui —— P2 当前形态的可视化页面（阶段 `_11` 新增的顶层模块）。

把 M1–M7 的全部机制搬进浏览器：事件流实时生长（M3）+ 每 step 渲染与装配单（M4）
+ 全工具面 edit/search/find/shell（M5/M7）+ 插件台账可见（M6）+ 配置驱动（M6）。

入口：`python webui/server.py [--profile dev|prod]`（本包自己的 `index.html`）。
基线的 `python -m harness.webui.server` 仍在——那是 M1–M3 形态的历史快照。

**路径引导**放在本 `__init__`：包被导入时（无论从阶段目录还是直接跑 server.py）
先把阶段根目录插进 sys.path，顶层包（harness/prompt/context/…）才找得到。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 阶段根目录 = 本包目录的上一级；插到 sys.path 首位前先判重（幂等）。
_STAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_STAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_STAGE_ROOT))

from webui.server import Handler, WebConfig, build_config, create_server, main

__all__ = ["Handler", "WebConfig", "build_config", "create_server", "main"]
