"""管理面板自述 —— 插件声明"我有一个管理页"（壳渲染成管理弹层的**一个标签页**）。

**解决的问题**：`_05` 收尾把"插件台账"做成了独立弹出面板，但那份面板是**壳里
写死的一页**：入口门卡、`/api/plugins`、快照的字段名、卡片怎么画，全在 app.js /
index.html 里。于是 `_06` 想让"工具管理"也出现在同一个面板里时，唯一的路是
再写一页——加第三个管理面就要改壳第三次。这正是"完全插件化"在展示层的假账。

**做法**：插件声明 `AdminSpec`，壳只做四件事：

    标签页     遍历声明 → 每个声明一个标签（label / order 由插件定）
    取数       GET 声明的 snapshot 地址（返回**通用快照形状**）
    画         groups → 卡片 → 按钮（按钮文字来自快照里的 actions 映射）
    提交       POST 声明的 action 模板（`{name}` / `{action}` 占位）

壳不认识"插件"也不认识"工具"——它只认识"分组、卡片、按钮、快照"这套形状。

**通用快照形状**（`_05` 的插件快照已经是这个形状，`_06` 的工具快照照它来）：

    {
      "groups": [ {"category": "分组键", "label": "分组名", "hint": "一句话",
                   "plugins": [ {"name", "status", "category",
                                 "tags": [{"text","kind","title"}],   # 可选
                                 "note": "备注",                       # 可选
                                 "actions": ["disable", ...],
                                 "blocked_reason": "为什么不能操作"} ]} ],
      "installable": [ {"name": "还没装的，可安装", "actions": ["install"]} ],
      "actions": {"disable": "停用", ...}      # 动作名 → 按钮文字
    }

字段名沿用插件快照的（`groups` / `plugins`）而不是另起一套：**一份渲染器**
同时服务两页，字段一漂移就会静默少画一块，所以宁可名字略不贴切也不分叉。

**降级规则（replace-by-key）**：壳内置一页**只读**的"插件"（直接读内核台账，
不依赖任何插件）。插件声明了同 key（`plugins`）的一页就**顶掉**它——于是
"没装 plugin-admin" 时界面自动退回只读页，而不是空白或报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 壳内置的只读降级页用的 key（声明同 key 即可替换它）。
BUILTIN_PLUGINS_TAB = "plugins"


@dataclass(frozen=True)
class AdminSpec:
    """一个管理页的自述：标签 + 取数地址 + 动作地址。

    - `snapshot`：GET 这个地址拿通用快照（`api/plugins`）；
    - `action`：POST 模板，`{name}` 与 `{action}` 会被替换
      （`api/plugins/{name}/{action}`）。

    两条都是**相对页面自身的路径**——插件的端点由壳（webui）挂载在它自己的
    服务器上（`components/shell/routes.py`），所以管理页与壳同源，不需要
    "服务的地址"这一层。这里只是把"从界面怎么调到它们"说清楚，让壳不必猜
    命名约定（猜命名 = 壳认识插件，禁止）。
    """

    #: 标签标识（也是 replace-by-key 的键）。
    key: str
    #: 标签上的文字（如 "插件"、"工具"）。
    label: str
    #: 弹层标题（切到这个标签时显示）。
    title: str = ""
    #: 标题下的一句说明（可选）。
    summary: str = ""
    #: 快照地址（相对服务根，不以 / 开头；壳自己拼服务地址）。
    snapshot: str = ""
    #: 动作地址模板（同上，含 `{name}` / `{action}`）。
    action: str = ""
    #: 页脚说明（**由插件给**：不同管理面的注意事项不一样——插件页说"基础不可卸"，
    #: 工具页说"停用只影响工具面"。壳没有默认措辞，不写就只显示通用那句）。
    foot: str = ""
    #: 排序权重（小的在前；同值按 key 字典序，顺序稳定）。
    order: int = 100

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("AdminSpec.key 不能为空")
        if not self.label:
            raise ValueError(f"管理页 {self.key!r} 必须有标签文字（label）")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "title": self.title or self.label,
            "summary": self.summary,
            "snapshot": self.snapshot,
            "action": self.action,
            "foot": self.foot,
            "order": self.order,
        }


def admins_of(service: Any) -> list[AdminSpec]:
    """从一个已提供的服务对象上读出它自述的管理页（没有则空列表）。

    **按结构判断，不用 isinstance**：类对象在不同"模块副本"里可能不是同一个
    （阶段目录各自持有同名包，测试的 conftest 会驱逐 `sys.modules`），
    `isinstance` 在这种情形下会静默返回 False——自述就这么无声无息地没了。
    判据改成"长得像不像"：有 `to_dict()` 且能序列化出 `key`。
    """
    raw = getattr(service, "admins", None)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [item for item in raw if _looks_like_admin(item)]
    return [raw] if _looks_like_admin(raw) else []


def _looks_like_admin(value: Any) -> bool:
    if not callable(getattr(value, "to_dict", None)):
        return False
    try:
        data = value.to_dict()
    except Exception:  # noqa: BLE001 —— 自述写坏了不该炸整个面板
        return False
    return isinstance(data, dict) and isinstance(data.get("key"), str) and bool(data["key"])


__all__ = ["BUILTIN_PLUGINS_TAB", "AdminSpec", "admins_of"]
