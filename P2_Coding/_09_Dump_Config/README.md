# _09_Dump_Config —— dump-config 与配置错误定位

P2 第 9 阶段（对应学习计划 **M6**）。本阶段**只加一个机制**：

> `--dump-config` 打印最终配置树与**每项来源**；配错启动即报错并**指出位置**。

**状态：✅ 已完成（2026-10-09）** ｜ 决策记录 [docs/decisions/0014](../../docs/decisions/0014-dump-config.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_08` 完成后的
`harness/` + `prompt/` + `context/` + `providers/` + `kernel/` + `config/` + `profiles/`）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `config/dump.py`（顶层） | ★ **本阶段新增的机制** | `render_tree` 渲染器（每项带来源）+ `source_summary` |
| `config/rows.py` | `_08` 的行与树 | ★ 加**来源台账** `sources`（字段级 provenance）+ 行级未知键校验 |
| `config/patch.py` | `_08` 的 patch 引擎 | ★ 层标签（label）+ 错误带层名/条目号 + id 拼写建议 |
| `config/profiles.py` | `_08` 的分层加载 | ★ 加载即填台账；CLI 补丁带序号标签 |
| `providers/plugins.py` | `_08` 的树→插件 | ★ `PLUGIN_CONFIG_KEYS` 键白名单 + 工厂错误加行定位 |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |

## 0) 现状基线

```bash
cd P2_Coding/_09_Dump_Config
python -m pytest -q        # 22 个基线用例 + 31 个本阶段用例 = 53
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `docs/cordis-primer.md`（loader configuration 节）
- `apps/cli/src/profile-boot.ts`（profile 解析与补丁叠加）

**做**：
- ✅ `dump-config`：输出可读配置树 + **每项来自哪一层**；
- ✅ 配置校验：未知行/未知插件名/未知参数键/类型错 → 启动即报错并**指出位置**（铁律 #8）。

**验收**：
- [x] dump-config 可读、显示来源层（`← base.yaml` / `← dev.yaml` / `← CLI#N x.yaml`）
- [x] 故意写错配置 → 启动即报错并指出位置（层名 + 条目序号；可疑处给拼写建议）
- [x] 基线 22 用例仍全绿；门禁绿（53 用例，ruff 通过）

## 2) 本阶段新增的东西

**来源台账（字段级 provenance，`config/rows.py`）**：`ConfigTree.sources` 记录
`行 id → 字段名 → 层标签`。**粒度到字段**——因为 patch 的语义是"给了就换、没给就沿用"：
同一行完全可能 name 来自 CLI 补丁、config 还来自 base。数据流经的每一步都在填台账：
`load_tree` 打上文件名、`apply_patch` 只为**它实际改过的字段**更新来源、
`load_profile` 给 CLI 补丁编号。

**渲染器（`config/dump.py`）**：

```
【profile = dev】共 4 行（行序 = 激活顺序；激活 4 行）
  1. llm
       name                           = llm:deepseek             ← CLI#1 pin.yaml
       config.model                   = deepseek-flash           ← CLI#1 pin.yaml
  2. fs
       name                           = fs:memory                ← dev.yaml
       config                         = {}                       ← dev.yaml
  4. toolbox
       name                           = toolbox                  ← base.yaml
       config.shell_timeout           = 15                       ← base.yaml
```

`source_summary` 另给一个**按层看**的视角（"这层贡献了哪些字段"）。

**四类配置错误的定位**（全部在启动/激活前 fail loud）：

| 错误 | 消息形状 |
|---|---|
| patch 指向不存在的行 | `my.patch.yaml 第 1 条 patch：行 id 'toolbx' 不存在（现有：…）；你是不是想改 'toolbox'？` |
| insert 撞已有 id | `my.yaml 第 1 条 insert：行 id 已存在：llm（insert 是「新增行」；想改已有行请用 patch）` |
| 未知插件名 | `行 'llm' 的 name 未知：'llm:fake2'（可用：…）；你是不是想写 'llm:fake'？` |
| 未知参数键 / 类型错 | `行 'toolbox'（toolbox）有未知参数：['shell_timeou']（可用：…）；你是不是想写 'shell_timeout'？` / `行 'toolbox' 的参数有问题：could not convert …` |

新增的 `PLUGIN_CONFIG_KEYS` 白名单让"参数拼错"不再静默失效——这是 `_08` 留下的
债务（当时 config 是自由字典），本阶段收掉。

## 3) 运行方法

```bash
cd P2_Coding/_09_Dump_Config
python -m pytest -q

# ★ 交互入口：dump 带来源；错误带定位
python chat.py --dump-config                     # dev profile：每项 ← 来源层
python chat.py --patch my.yaml --dump-config     # 叠一层：改过的字段标 CLI#1 my.yaml
python chat.py --profile prod --dump-config      # prod：三行来自 prod.yaml
python chat.py --patch 坏.yaml                    # 配错 → [配置错误] + 层/条/建议（退出码 2）
python chat.py --profile dev --ask "帮我算 2+3"    # 正常会话照常
# 会话内命令同 `_08`：/config（现在是带来源的树）；/ctx；/prompt；/unload；/exit

# 脚本化演示（离线）
python demo.py    # 0a 来源追踪（多层叠加后逐项看来历 + 按层汇总）；
                  # 0b 六类错误的定位文案；0c 分层与一行换 provider 的回归
```

**真实对话里能看到什么**：`--dump-config` 让"这行是谁定的"一眼可读；
一行 CLI 补丁改过 llm 行后，dump 里该行来源变成 `CLI#1 xxx.yaml` 而其余行
原样标着 `dev.yaml` / `base.yaml`——**"换一行"与"从哪换的"都在同一屏里**。

## 4) 完成后的去向

**M6 完成**（组合与配置：插件 effect → 配置分层 → 可见与可定位）。
下一阶段 `_10_Rpc_Transport` 进入 **M7 服务化**：stdio JSON-RPC
（`initialize` / `session.prompt`），harness 从"一个进程里的程序"变成"可被连接的服务"。
