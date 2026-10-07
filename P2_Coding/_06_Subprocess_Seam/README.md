# _06_Subprocess_Seam —— SubprocessService 接缝 + shell 工具

P2 第 6 阶段（对应学习计划 **M5**）。本阶段**只加一个机制**：

> 为命令执行建三角色接缝（spawn / 捕获输出），加一个 `shell` 工具——工具只依赖抽象。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/providers/ | FileSystem 接缝 | ★ 加 SubprocessService + LocalSubprocess |
| harness/tools/ | calculate / read / write / list | 加 shell 工具（捕获 stdout/stderr/退出码） |

## 0) 现状基线

```bash
cd P2_Coding/_06_Subprocess_Seam
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/subprocess/subprocess/src/index.ts`
- `packages/shell/shell` + `bash-local/` + `tool-bash/`（接缝的教科书样例）

**做**：
- `SubprocessService` 定义（spawn / 捕获）；本地实现；
- `shell` 工具（超时、退出码、stdout/stderr）。

**验收**：
- 捕获 stdout / stderr / 退出码
- 超时能被处理
- 工具只依赖抽象
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0011.md`。

## 2) 运行方法

```bash
cd P2_Coding/_06_Subprocess_Seam
python -m pytest -q
python -m harness run "用 shell 看看当前目录" --fake
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

M5 完成。下一阶段 `_07_Plugin_Effect` 进入 M6 组合。
