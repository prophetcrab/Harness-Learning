# _04_Filesystem_Seam —— FileSystem 接缝（LocalFS / MemoryFS）

P2 第 4 阶段（对应学习计划 **M5**）。本阶段**只加一个机制**：

> 把文件工具从「直接操作 pathlib」重构成 **FileSystem 三角色接缝**，并给出两个可互换实现。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/providers/ | 占位（只有 __init__.py） | ★ FileSystem 定义 + LocalFS + MemoryFS |
| harness/tools/workspace.py | 直接用 pathlib 读写 | 只依赖 FileSystem 抽象 |
| harness/mini.py | 直接传 workspace 路径 | 在显式 resolve 步骤选定 FileSystem provider |

## 0) 现状基线

```bash
cd P2_Coding/_04_Filesystem_Seam
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/fs/fs/src/index.ts`（定义/实现/消费者）
- `docs/capability-seams.md`（服务-实现-消费者那张图）

**做**：
- `FileSystem` 定义（read / write / edit / 列表）；**LocalFS** 与 **MemoryFS** 两个实现；
- 工具只依赖抽象——`harness/tools/` 里不再出现 `pathlib` 读写；
- 单槽服务：同一能力重复注册直接报错（fail loud）；provider 在**显式 resolve** 处选定（铁律 #6）。

**验收**：
- **同一套工具测试在 `local` 与 `memory` 两个 provider 下都全绿**（换实现不改测试）
- 重复注册同一能力报错
- provider 在显式 resolve 处选定
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0009.md`。

## 2) 运行方法

```bash
cd P2_Coding/_04_Filesystem_Seam
python -m pytest -q
python -m harness run "把 hello 写到 notes/a.txt" --fake
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

`_05_Workspace_Jail` 再加一个**策略型** provider（越界写被拒）。
