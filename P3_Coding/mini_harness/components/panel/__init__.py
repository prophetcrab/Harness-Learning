"""panel 构件 —— 插件的"界面自述"（`_05` 收尾补齐耦合缺口）。

`spec.py` 定义形状；本包把它转向外部消费。
"""

from components.panel.spec import BadgeKind, PanelSpec, panels_of

__all__ = ["BadgeKind", "PanelSpec", "panels_of"]
