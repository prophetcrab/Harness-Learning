# _08_Profile_Layers —— profiles YAML 分层与 patch

P2 第 8 阶段（对应学习计划 **M6**）。本阶段**只加一个机制**：

> 能力组合从代码变成**配置数据**：`profiles/*.yaml` 有序层 + 按 id patch；**一行 patch 换 provider**。

**状态：✅ 已完成（2026-10-09）** ｜ 决策记录 [docs/decisions/0013](../../docs/decisions/0013-profile-layers.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_07` 完成后的
`harness/` + `prompt/` + `context/` + `providers/` + `kernel/`，本阶段新增顶层 `config/`
与 `profiles/`）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `config/`（顶层） | ★ **本阶段新增的机制** | 配置行/树 + patch 引擎 + 分层加载（纯数据） |
| `profiles/`（顶层） | ★ 配置数据 | `base.yaml` + `dev.yaml` + `prod.yaml` + `user.patch.yaml` |
| `providers/plugins.py` | ★ 接线 | `PLUGIN_FACTORIES` 工厂注册表 + `boot_tree`（树 → 插件 → ctx） |
| `kernel/` / `prompt/` / `context/` | 前序机制 | 未改动 |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |

## 0) 现状基线

```bash
cd P2_Coding/_08_Profile_Layers
python -m pytest -q        # 22 个基线用例 + 45 个本阶段用例 = 67
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/bundle/base/cordis.patch.yml`（底座补丁长什么样：行有 id/name/config/disabled）
- `apps/cli/src/profile-boot.ts`（补丁叠加顺序：bundle 层 → profile → --patch 覆盖层）

**做**：
- ✅ `profiles/*.yaml`：**有序层 + 按 id patch**（替换整块 config 或 insert 新行）；
- ✅ 叠加顺序：`base → profile patch → 用户 patch → CLI --patch`；
- ✅ 两个 profile：`dev`（FakeLLM + MemoryFS）、`prod`（DeepSeek + 本地）。

**验收**：
- [x] 同 base、两 profile 产出**不同且可读**的树（差异可枚举：llm/fs/subprocess 三行）
- [x] 换 LLM provider **只改一行**（CLI 补丁把 dev 的 llm 行换成 deepseek，其余不动）
- [x] 叠加顺序正确（CLI 覆盖 profile；CLI 多层按序后写胜出）
- [x] 基线 22 用例仍全绿；门禁绿（67 用例，ruff 通过）

## 2) 本阶段新增的东西

**顶层 `config/` 包**（纯数据层，不依赖 kernel/providers）：

| 文件 | 内容 |
|---|---|
| `rows.py` | `ConfigRow`（id/name/config/disabled）+ `ConfigTree`（有序、id 唯一、active_rows） |
| `patch.py` | `Patch`/`PatchRow` + `apply_patch`：按 id **整块替换**（不合并）+ insert 追加 |
| `profiles.py` | `load_tree`/`load_patch`/`load_profile`/`compose`：分层加载与叠加 |

**配置数据 `profiles/`**：

```
base.yaml          底座：4 行（llm / fs / subprocess / toolbox）+ 中性默认（不联网、不碰盘）
dev.yaml           dev 补丁：把三行换成离线实现（fake / memory / scripted）
prod.yaml          prod 补丁：把三行换成真实实现（deepseek / local / local）
user.patch.yaml    用户层示例（空壳；个人覆盖放这里，按层序夹在 profile 与 CLI 之间）
```

**激活（`providers/plugins.py`）**：`PLUGIN_FACTORIES`（行 name → 插件工厂）+
`boot_tree(tree) = 构造全部插件 → load_plugins 装载`。两步分离：**配置错误在构造阶段
全部拦下**（未知 name、非法参数），此时没有任何东西被登记；装载失败由 kernel 回卷。

**三条语义（本阶段的核心）**：

1. **行是身份，name 是实现**。patch 用 id 定位行，改 name 就是换实现——
   "换 LLM provider 只改一行"因此是字面事实（一个 YAML 片段三行字）。
2. **整块替换，不做字段合并**。patch 给了 config 就整个换（`config: {}` 是清空）；
   没给就沿用。字段级合并会让"这行最终是什么"散在多个层里；dsh 的原话是
   "a patch replaces the targeted row's whole config rather than merging into it"。
3. **行序 = 激活顺序；禁用行留在树里**。`disabled: true` 使行不激活但不从树里消失
   （临时关掉某能力时，diff 只有一行 true）。

## 3) 运行方法

```bash
cd P2_Coding/_08_Profile_Layers
python -m pytest -q

# ★ 交互入口：一切由配置驱动
python chat.py                                  # dev profile（离线：fake + memory + 剧本）
python chat.py --dump-config                    # 打印最终配置树（不启动）
python chat.py --profile prod                   # prod profile（DeepSeek + 真实磁盘/进程）
python chat.py --patch my.yaml                  # 叠一层 CLI 补丁（可多次，后写胜出）
python chat.py --patch pin-model.yaml --ask "..."  # 例：钉死模型的一行补丁
# 会话内命令：/config 配置树；/ctx 插件台账；/prompt 装配单；/unload 回卷演示；/exit

# 脚本化演示（离线）
python demo.py    # 0a 四层叠加逐步看树变化；0b 一行 patch 换 provider；
                  # 0c 配置错误的 fail loud；主故事（dev profile）+ 整体卸载
```

**真实对话里能看到什么**：`--profile prod` 是一个"真实世界"组合（DeepSeek 回复、
`shell` 真执行、文件真落盘）；`--profile dev` 全离线；而两者之间只差三行配置。
`--patch` 可以只覆盖其中一行——例如让 dev 的 LLM 换成真实 DeepSeek、其余保持离线。

## 4) 完成后的去向

`_09_Dump_Config` 让最终配置**可见**、错误**可定位**：`dump-config` 会交代
"这一行最终值来自哪一层"（本阶段 `--dump-config` 已能打印树，`_09` 补来源追溯与
错误定位——"第 3 层第 2 行 patch 指向了不存在的 id"这种级别的诊断）。
