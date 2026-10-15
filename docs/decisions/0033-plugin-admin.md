# 0033 插件管理：把"哪些插件是地基"变成可点击的事实

- 日期：2026-10-15
- 阶段：P3_Coding `_05_Compaction`（收尾）
- 状态：已采纳

## 背景

用户提问："现在的代码架构符合我们对于完全是插件的原预期吗？"

查完的答案是**能力上符合、可管理性上不符合**：插件已经能列出来（`list_plugins`），
但"列出来"只回答了"有什么"，没回答"哪些能删"。

三件事缺失：

1. **没有分类**。`llm`（模型）和 `clock`（一个演示时钟）在台账里长得一样。
   可实际上前者卸了应用就不成立、后者随时可以加。这个区别只存在于人的脑子里。
2. **只能卸载，不能停用**。想临时关掉用量统计？只能 `unload_plugin`——但那会
   把插件从账本里删掉，想再开回来得重新 `install`（`install_plugin` 是唯一引入
   新代码的路径，走人工审批）。**为了"暂时不要"而走"引入代码"的审批流程，说不通。**
3. **开关在模型手里**。模型有 `load_plugin` / `unload_plugin` 工具，而**坐在
   浏览器前面的人没有按钮**。人想看"现在装了些什么、关掉一个试试"，得让模型代劳。

## 决定

### ① 分类是插件的**声明**，策略是消费方的**解释**

内核新增 `category` 声明（`base` / `capability` / `other`，缺省 `other`）：

```python
class ToolsPlugin:
    name = "tools"
    inject = (FS_SERVICE,)
    category = "base"
```

**内核只"读出来、记进台账"，不解释它的含义。** 为什么不让内核直接说
"base 不可卸载"？因为那是**产品策略**，不是机制。内核管"能不能"，`plugin_admin`
管"该不该"。今天"基础插件不可卸载"是策略，明天想加"企业版允许卸载"，
只改 `plugin_admin.py`，内核一行不动。

未声明取 `other` 而不是 `base`：**保守取向**——不声称自己是基础设施。
漏声明的后果是"这个插件看起来可以卸载"（人看一眼就发现），而不是
"一个真·地基插件被悄悄打上了不可卸载的标签"（没人会发现）。

### ② `disable` 与 `unload` 是两件事，内核必须都提供

- `disable` = 回卷 effect（服务下线、订阅撤销），但**注册留在账本里**；
  同一个插件对象上 `enable` 就能原地活回来。
- `unload` = 从账本里**消失**，要回来得重新安装。

`disable` 的状态必须**先设、后回卷**，否则有个隐蔽的坑：

```python
# 错误写法：回卷完再改状态
self._deactivate(record)          # 里面有 settle → 看它还是 pending
record.status = "disabled"
```

回卷过程中触发的 `settle` 会发现"这个插件还是 pending、依赖也齐"，
**当场把它重新激活**，`enable` 时就会撞上 `DuplicateSlotError`。
所以 `_deactivate` 收一个 `status` 参数，在回卷**之前**就把状态钉死。

### ③ 白名单只有一份，两个消费者共用

`install` 能装什么，模型侧（`self_tools` 的 `install_plugin` 工具）和界面侧
（插件管理的"可安装"列表）**必须一致**。两份名单一定会漂移——所以提到
`plugins/catalog.py`：

```python
DEFAULT_CATALOG_NAMES = ("clock", "webui", "rpc")

def default_catalog() -> dict[str, PluginFactory]:
    ...
```

测试按**内容**断言而非对象身份（本项目的阶段目录各持同名包、conftest 会
驱逐 `sys.modules`，`isinstance` 会诡异地失败——这个坑踩过三次）。

### ④ 按钮由服务下发，壳依然零知识

`plugin_admin` 按 `(分类, 状态, 是否在可安装名单)` 算出**该给哪些按钮**：

| 分类 | 状态 | 按钮 |
|---|---|---|
| base | active | （无）"基础插件：当前应用不可卸载" |
| base | 非 active | （无）"依赖未就绪，等它自己激活" |
| capability/other | active | 停用 · 卸载 |
| capability/other | disabled / failed | 启用 · 卸载 |
| capability/other | pending | 卸载（附"等待依赖：…"） |

**这个判断在插件里，不在壳里。** 壳只做两件事：把服务给的按钮画出来（`action`
字符串 → 按钮 → POST 到端点）、把返回的 `message` 显示在提示条上。
以后改策略（比如"其他插件可删、能力插件要二次确认"），改 `plugin_admin.py`，
`app.js` / `index.html` 一行不动——这是 0032 那两条扩展点的第一次真实使用。

路由同样是自述的：

```python
def _admin_routes() -> tuple[RouteSpec, ...]:
    return (
        RouteSpec("GET", "/api/plugins", list_plugins, order=3),
        RouteSpec("POST", "/api/plugins/<name>/<action>", act, order=4),
    )
```

## 代价

- **多了一层间接**：看"某个按钮为什么出现"，要跳
  `plugin_admin.py` 的策略表，而不是直接在 HTML 里读到。
  这是刻意的——代价换来的是"改策略不用碰界面"。
- **`category` 要人工声明**：15 个插件各加一行。漏了不会报错（取默认 `other`），
  表现为"这个地基插件居然显示可卸载"。靠人眼与测试兜（`test_s05_plugin_admin`
  逐个断言默认插件的分类）。

## 实测

浏览器里四条路径都走过（截图留在会话存档）：

- 初始：基础插件 7（无按钮）· 能力插件 7（停用/卸载）· 可安装 2
- **安装 clock** → 提示条"已安装 clock"，台账 14/15 活跃，长出新分组"其他插件"
- **停用 token-usage** → "已停用 token-usage"，13/14 活跃，顶部工具数 13 → 12
- **启用 token-usage** → "已启用 token-usage"，15/15 活跃，工具数回到 14
- **卸载 clock** → "已卸载 clock"，14/14 活跃，分组回到三个、clock 回到"可安装"

## 附带修掉的两处

1. **`#banner` 元素第三次丢失**（前两次分别在 `_06` 与页面拆分时）。
   `showBanner()` 只在人点插件按钮时才被调用，所以页面看着一切正常，
   一点"安装"就 `null.textContent` 炸掉，报错还只在浏览器控制台里。
   现在有了静态契约测试：`app.js` 用到的每个 id，`index.html` 必须声明
   （去掉 `#banner` 会立刻失败——已验证）。
2. **测试往仓库里写垃圾**：`test_shared_catalog_is_loadable` 用了相对路径
   `Path("__none__")`，在仓库根建出空目录 `__none__/s`、`__none__/ws`。
   空目录 `git status` 看不见，只有人眼能发现。改用 `tmp_path`。

## 相关

- [0031 插件声明纪律](0031-plugin-declaration-discipline.md)：同样是把约定变成不变量
- [0032 壳的两条扩展点](0032-shell-surfaces.md)：本决策的按钮/路由自述是它的首次实战
- [0012 插件与 effect](../decisions/0012-plugin-effect.md)：`disable` 的回卷复用同一套 effect 机制
