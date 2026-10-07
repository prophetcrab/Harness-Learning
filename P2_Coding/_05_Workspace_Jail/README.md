# _05_Workspace_Jail —— WorkspaceJailFS 策略 provider

P2 第 5 阶段（对应学习计划 **M5**）。本阶段**只加一个机制**：

> 再加一个 `FileSystem` 实现——**越出工作目录的写被直接拒绝**（策略型 provider）。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/providers/ | _04 的 FileSystem 接缝 | ★ WorkspaceJailFS（沙箱策略） |

## 0) 现状基线

```bash
cd P2_Coding/_05_Workspace_Jail
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/fs/fs` 的策略/沙箱部分
- `packages/sandbox/`

**做**：
- `WorkspaceJailFS`：路径 resolve 后必须落在工作区内，越界返回**结构化错误**；
- 能在 resolve 步骤与 local/memory 互换。

**验收**：
- jail 越权返回的错误结构与其它工具错误**一致**（同样是 `{"error": ...}`）
- 审批放行也拦（沙箱是第二道防线）
- local / jail / memory 三种实现可切换
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0010.md`。

## 2) 运行方法

```bash
cd P2_Coding/_05_Workspace_Jail
python -m pytest -q
python -m harness run "把 x 写到 ../escape.txt" --fake
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

`_06_Subprocess_Seam` 给「命令执行」也建一条接缝。
