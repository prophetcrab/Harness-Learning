# _04_Filesystem_Seam —— FileSystem 接缝（LocalFS / MemoryFS）

P2 第 4 阶段（对应学习计划 **M5**）。本阶段**只加一个机制**：

> 把文件工具从「直接操作 pathlib」重构成 **FileSystem 三角色接缝**，并给出两个可互换实现。

**状态：✅ 已完成（2026-10-08）** ｜ 决策记录 [docs/decisions/0009](../../docs/decisions/0009-filesystem-seam.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_03` 完成后的
`harness/` + `prompt/` + `context/`，本阶段新增顶层 `providers/`）。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `providers/`（顶层） | ★ **本阶段新增的机制** | FileSystem 定义 + LocalFS / MemoryFS + 单槽服务 + 接缝工具 |
| `prompt/` / `context/`（顶层） | `_01`–`_03` 的机制 | 未改动（接缝与提示词互不相干） |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |
| `chat.py` / `demo.py` / `tests/` | 入口与测试 | `--fs local\|memory`；第 0 节接缝对照演示 |

## 0) 现状基线

```bash
cd P2_Coding/_04_Filesystem_Seam
python -m pytest -q        # 22 个基线用例 + 54 个本阶段用例 = 76
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/fs/fs/src/index.ts`（定义/实现/消费者）
- `docs/capability-seams.md`（服务-实现-消费者那张图）

**做**：
- ✅ `FileSystem` 定义（read / write / edit / 列表）；**LocalFS** 与 **MemoryFS** 两个实现；
- ✅ 工具只依赖抽象——接缝工具里不再出现 `pathlib` 读写；
- ✅ 单槽服务：同一能力重复注册直接报错（fail loud）；provider 在**显式 resolve** 处选定（铁律 #6）。

**验收**：
- [x] **同一套工具测试在 `local` 与 `memory` 两个 provider 下都全绿**（参数化 fixture）
- [x] 重复注册同一能力报错；未注册就 resolve 报错
- [x] provider 在显式 resolve 处选定（`ServiceContainer.resolve`）
- [x] 结构化错误：`FsError` → `{"error": 人话, "code": 稳定码}`（铁律 #7）
- [x] 基线 22 用例仍全绿；门禁绿（76 用例，ruff 通过）

## 2) 本阶段新增的东西

**顶层 `providers/` 包**（三角色 + 服务容器）：

| 文件 | 角色 | 内容 |
|---|---|---|
| `filesystem.py` | **Definition** | `FileSystem` 协议（read/write/edit/列表）+ `FsError` + 5 个稳定错误码 |
| `local.py` | Provider | `LocalFS`：真实磁盘（root 下的路径解析） |
| `memory.py` | Provider | `MemoryFS`：字典支撑，进程结束即消失；目录隐式 |
| `service.py` | 服务容器 | `ServiceContainer`：单槽 register / 显式 resolve；重复注册 fail loud |
| `toolbox.py` | **Consumer** | `build_filesystem_registry(fs)`：read/write/list 工具只认协议 |

**接线变化**：`context.wiring.open_context_harness(...)` 新增 `tool_registry` 参数
（缺省仍用基线工具面）；`chat.py` 加 `--fs local|memory`——先 register 再 resolve，
**"用哪个实现"在解析点决定**（铁律 #6）。

**两处语义升级**（工具面与基线保持同名同 schema）：

1. **错误模型**：provider 失败抛 `FsError(code, message)`；工具层翻译成
   `{"error": ..., "code": "FS_NOT_FOUND"}` 回给模型——程序按码分支、模型读人话。
2. **`edit` 操作**：新增的原子编辑语义——待替换文本**恰好出现一次**才替换；
   0 次报 `FS_EDIT_NO_MATCH`、多次报 `FS_EDIT_AMBIGUOUS`（拒绝猜测）。

**刻意的边界**：接缝定义**不含策略**——"路径是否越界"不由基础 provider 管，
留给 `_05` 的策略型 provider（WorkspaceJailFS）。本阶段的两个实现都"来者不拒"。

## 3) 运行方法

```bash
cd P2_Coding/_04_Filesystem_Seam
python -m pytest -q

# ★ 交互入口：换一个 provider 就换一个"文件世界"
python chat.py                              # 真实 API + LocalFS（默认）
python chat.py --fs memory                  # 真实 API + MemoryFS：磁盘零痕迹
python chat.py --fake --fs memory --ask "把 hello 写到 notes/a.txt 再读回来"
python chat.py --fs local --ask "..." --dump-prompt    # 附带装配单（_03 机制）
# 会话内命令：/fs 看当前 provider；/prompt 装配单；/context 生效文本；/exit

# 脚本化演示（离线，跑完整故事线）
python demo.py        # 0a 接缝对照（同操作两实现行为一致）
                      # 0b 单槽服务 fail loud；0c 同一段对话两 provider 结果逐字段相同
```

**两次真实对话里能看到什么**：`--fs memory` 下模型写入读回一切正常，但磁盘上
一个字都没有；`--fs local` 下同样操作真的落盘。**同一套工具、同一套日志**，
差别只有 provider 那一行的选择——这就是"换 provider 换产品"。

## 4) 完成后的去向

`_05_Workspace_Jail` 再加一个**策略型** provider（`WorkspaceJailFS`：越出工作目录的
写被拒）——接缝不动，只加一个实现；`_06_Subprocess_Seam` 给命令执行也建一条接缝。
