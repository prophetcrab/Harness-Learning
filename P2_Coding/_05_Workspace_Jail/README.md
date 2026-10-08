# _05_Workspace_Jail —— WorkspaceJailFS 策略 provider

P2 第 5 阶段（对应学习计划 **M5**）。本阶段**只加一个机制**：

> 再加一个 `FileSystem` 实现——**越出工作目录的写被直接拒绝**（策略型 provider）。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0010](../../docs/decisions/0010-workspace-jail.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_04` 完成后的
`harness/` + `prompt/` + `context/` + `providers/`，本阶段给 `providers/` 加一个策略实现）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `providers/jail.py`（顶层） | ★ **本阶段新增的机制** | `WorkspaceJailFS`：策略型 provider（改动围栏） |
| `providers/filesystem.py` | `_04` 的接缝定义 | 加一个错误码 `FS_SANDBOX_DENIED`（对齐 dsh） |
| `prompt/` / `context/`（顶层） | `_01`–`_03` 的机制 | 未改动 |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |

## 0) 现状基线

```bash
cd P2_Coding/_05_Workspace_Jail
python -m pytest -q        # 22 个基线用例 + 53 个本阶段用例 = 75
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/fs/fs-sandbox/`（`FS_SANDBOX_DENIED`；confine mutations, preserve reads）
- `packages/sandbox/sandbox-policy/`（策略是独立一层，不烧进基础 provider）

**做**：
- ✅ `WorkspaceJailFS`：路径解析后必须落在工作区内，越界返回**结构化错误**；
- ✅ 能在 resolve 步骤与 local / memory 互换（装饰器：可叠在任意机制之上）。

**验收**：
- [x] jail 越权返回的错误结构与其它工具错误**一致**（同样是 `{"error", "code"}`，
      码为 `FS_SANDBOX_DENIED`）
- [x] 审批放行也拦（沙箱是第二道防线）
- [x] local / jail / memory 三种实现可切换；**区内操作三边结果逐字段相同**
- [x] 基线 22 用例仍全绿；门禁绿（75 用例，ruff 通过）

## 2) 本阶段新增的东西

**`providers/jail.py` —— WorkspaceJailFS**（装饰器形态，与机制型 provider 的关系）：

```
            ┌──────────────────────────────────────────────┐
            │  机制型 provider：文件"怎么"存               │
            │  LocalFS（磁盘） / MemoryFS（内存）          │
            └──────────────────────────────────────────────┘
                              ▲ 被包住
            ┌──────────────────────────────────────────────┐
            │  策略型 provider：改动"允许"落在哪          │
            │  WorkspaceJailFS(base)                       │
            └──────────────────────────────────────────────┘
```

三条边界（守住这三点，围栏才是对的）：

1. **只拦改动，不拦读**：`write_text` / `edit_text` 设卡；`read_text` / `list_files`
   透传——与 dsh `fs-sandbox` 的边界一致（confine mutations, preserve reads）。
2. **判据 = 词法规范化后的相对路径**：`..` 逃逸、绝对路径、盘符路径、反斜杠形态
   全部拒绝；`notes/../a.txt` 这类"绕圈但仍在区内"的路径规范化后**放行**。
3. **真实磁盘再加物理复核**：底层有 `root` 时（如 LocalFS），把规范化路径拼回
   真实根再 `resolve()`，仍须落在根内——挡住符号链接指向区外的绕行；内存 provider
   没有真实根，词法检查就是全部。

拒绝抛 `FsError(FS_SANDBOX_DENIED, ...)`；工具层照旧翻译成
`{"error": 人话, "code": "FS_SANDBOX_DENIED"}` —— 与"文件不存在""这不是目录"等
**完全同构**（调用方按 code 分支即可）。

## 3) 运行方法

```bash
cd P2_Coding/_05_Workspace_Jail
python -m pytest -q

# ★ 交互入口（三选一）：--fs local|jail|memory
python chat.py --fake --fs jail --ask "把 x 写到 ../escape.txt"   # 越界 → 被拒
python chat.py --fake --fs local --ask "把 x 写到 ../escape.txt"  # 对照：真的写出去
python chat.py --fs jail --ask "把 hello 写到 notes/a.txt"        # 区内照常（真实 API）
# 会话内命令：/fs 看当前 provider；/prompt 装配单；/context 生效文本；/exit

# 脚本化演示（离线，跑完整故事线）
python demo.py        # 0a 围栏（5 种越界形态 / 区内 / 读透传 / 叠在 MemoryFS 上）
                      # 0b 单槽服务；0c 三 provider 对照 + 越界：local 放行 vs jail 拒绝
                      # 1) 主故事：区内写（审批）→ 越界写（审批也批准→仍被拦）→ resume
```

**真实对话里能看到什么**：`--fs jail` 下模型尝试越界写会收到结构化拒绝
（`FS_SANDBOX_DENIED` + 人话原因），区内写入一切照常；`--fs local` 下同样的越界
请求**没有围栏拦**（如果模型愿意执行的话）。顺带一个真实观察：**模型自己往往会
"先自我审查"**（提示词里写了工作区约定，它常主动换路径）——围栏因此更像"第二道
防线"：防的是模型判断失误、被诱导或忽略约定的情况。

## 4) 完成后的去向

`_06_Subprocess_Seam` 给「命令执行」也建一条接缝——同一套"Definition + Provider +
Consumer + 策略分层"的模式，换一个能力再练一遍（届时与 fs 接缝对照）。
