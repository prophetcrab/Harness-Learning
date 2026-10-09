# _07_Plugin_Effect —— 插件协议与 effect（ctx + disposer）

P2 第 7 阶段（对应学习计划 **M6**）。本阶段**只加一个机制**：

> 极简插件协议 `Plugin.setup(ctx) -> disposer`；**注册即 effect**，卸载时自动回卷。

**状态：✅ 已完成（2026-10-09）** ｜ 决策记录 [docs/decisions/0012](../../docs/decisions/0012-plugin-effect.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_06` 完成后的
`harness/` + `prompt/` + `context/` + `providers/`，本阶段新增顶层 `kernel/`）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `kernel/`（顶层） | ★ **本阶段新增的机制** | `Context`（注册中心 + effect 台账）+ `Plugin` 协议 + 装载器 |
| `providers/plugins.py` | ★ 接线 | 把 fs/subprocess/toolbox 装进 ctx 的三个插件 |
| `prompt/` / `context/`（顶层） | `_01`–`_03` 的机制 | 未改动（与 kernel 互不相干） |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |

## 0) 现状基线

```bash
cd P2_Coding/_07_Plugin_Effect
python -m pytest -q        # 22 个基线用例 + 35 个本阶段用例 = 57
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `docs/cordis-primer.md` —— "Registrations are reversible effects"（`ctx.effect()`）
- Cordis 五条核心思想（插件 = 有 setup 的对象；context = 服务仓库；注册可回卷…）

**做**：
- ✅ `Context`（插件共享的注册中心：命名槽位 + effect 台账）；
- ✅ `Plugin.setup(ctx) -> disposer`；注册即 effect，卸载时 disposer **自动回卷**（铁律 #3）；
- ✅ 重复注册同一槽位直接报错（fail loud）。

**验收**：
- [x] setup 返回 disposer（或注册期 `provide` 自动登记撤销）
- [x] 卸载后注册物**自动撤销**（回卷）——含嵌套 effect（子先父后）与故障回卷
- [x] 重复注册报错（槽位与 effect 名都 fail loud）
- [x] 基线 22 用例仍全绿；门禁绿（57 用例，ruff 通过）

## 2) 本阶段新增的东西

**顶层 `kernel/` 包**（两件套）：

| 文件 | 内容 |
|---|---|
| `context.py` | `Context`：命名槽位（`provide`/`retract`/`require`）+ **effect 台账**（`effect`/`register`/`unload`）+ effect **树**（子先父后回卷） |
| `plugin.py` | `Plugin` 协议（有 `setup(ctx)` 的对象，或普通函数）+ `load_plugin`/`load_plugins`（批量装载，失败回卷已装部分） |

**三条语义（本阶段的核心）**：

1. **注册即 effect**：在某个 effect 的装载期调用 `ctx.provide(...)`，这次登记会**自动**
   挂上"撤销槽位"的 disposer——插件不用自己写撤销动作，卸载/装载失败/整体拆除时
   槽位都会自动清掉。这是铁律 #3 的字面落实。
2. **effect 成树**：装载期注册的其它 effect 自动成为当前 effect 的**子 effect**；
   卸载父 effect 时，**子先父后**逐层回卷（同级后进先出）。"插件内部又注册了几个
   小 effect"这种结构能整体、按正确顺序拆干净。
3. **故障不留半装状态**：装载中途崩溃 → 已登记的（含已 provide 的槽位）全部回卷，
   异常继续抛出；某个 disposer 抛错 → **继续回卷其余部分**，最后把第一个异常抛出去。
   批量装载（`load_plugins`）同理：第二个插件装失败，第一个也被干净地拆掉。

**接线（`providers/plugins.py`）**：三个安装函数/插件对象——
`make_fs_plugin` / `make_subprocess_plugin` / `make_toolbox_plugin`，
以及一条龙的 `boot_toolbox(fs, shell_service, ...) -> (ctx, 工具面)`。
`install_toolbox` 只 `ctx.require` 槽位：**依赖缺失在装载时 fail loud**，
而不是运行到一半才发现。

**与 `ServiceContainer` 的关系**：`_04` 的单槽容器是"能力容器"的最小版（register/
resolve）；本阶段的 `Context` 把"登记"升级成"可回卷的装配台账"。两者并存——
旧入口（`_04`–`_06` 的 `build_toolbox` 直通）未动，新入口（插件装载）是推荐路径。

## 3) 运行方法

```bash
cd P2_Coding/_07_Plugin_Effect
python -m pytest -q

# ★ 交互入口：装配走插件，退出时看回卷
python chat.py --fake --ask "用 shell 看看目录"     # 离线（命令走剧本）
python chat.py --shell local --ask "用 shell 看看目录"   # 真实执行（需审批）
python chat.py                                     # 交互模式
# 会话内命令：/ctx 看插件台账与槽位；/unload 现场卸载 toolbox 插件看回卷；
#            /prompt 装配单；/fs、/shell 当前 provider；/exit（自动整体卸载）

# 脚本化演示（离线，跑完整故事线）
python demo.py        # 0a effect 装载/卸载回卷与 LIFO；
                      # 0b effect 树（子先父后）与故障回卷；
                      # 0c 装配对照（手工登记 vs 插件装载）；主故事 + 结束时整体卸载
```

**真实对话里能看到什么**：一切照旧（命令/文件都好使），但退出时多一段"回卷对照"——
装载时有 fs/subprocess/toolbox 三个槽位，整体卸载后全部清空；交互模式里 `/unload`
可以现场卸载一个插件、看它装的东西被撤销、再装回去。

## 4) 完成后的去向

`_08_Profile_Layers` 把「装配」写成配置层：`profiles/*.yaml` 有序层 + 按 id patch，
**一行 patch 换 provider**——插件台账（本阶段）将变成"由配置组装"的目标。
