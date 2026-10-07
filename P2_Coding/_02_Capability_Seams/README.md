# _02_Capability_Seams —— M5：能力接缝（Provider 替换）

P2 第二个阶段，对应学习计划 **M5**。目标：把"能用"的文件/命令工具重构成
**三角色接缝（Definition + Provider + Consumer）**，体验"换 provider 换产品"。

## 本阶段是自包含的

本目录带一份**完整的 `harness/` 副本**。这就是 P2 的约定：阶段之间不共享代码，
每个阶段目录都能单独跑、单独读。本阶段的起点应当是 `_01` 完成后的代码
（若 `_01` 已完成，把它的 `harness/` 整体复制过来，再在 `providers/` 上开工）。

`harness/` 里与本阶段相关的部分：

| 子包 | 现状 | 本阶段要做的 |
|---|---|---|
| `harness/providers/` | 占位（只有 `__init__.py`） | ★ 写 `FileSystem` / `SubprocessService` 抽象 + 本地实现 |
| `harness/tools/workspace.py` | 直接操作 `pathlib` 的 read/write/list | 改为**只依赖 `FileSystem` 抽象** |
| `harness/tools/builtin.py` | 用 `pathlib` 的 `write_file`/沙箱 | 沙箱逻辑上移为 `WorkspaceJailFS` provider |
| `harness/mini.py` | 装配时直接给 workspace 路径 | 装配时**显式 resolve** provider |

## 0) 现状基线（动手前先确认它绿的）

```bash
cd P2_Coding/_02_Capability_Seams
python -m pytest -q        # 22 个用例，必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/shell/shell/src/index.ts` + `bash-local/` + `tool-bash/`（接缝的教科书样例）
- `packages/fs/fs/src/index.ts`；`packages/subprocess/subprocess/src/index.ts`
- `docs/capability-seams.md`（服务-实现-消费者那张图）

**做**：
- `harness/providers/`：
  - 抽象 `FileSystem`（read / write / edit / 列表）与 `SubprocessService`（spawn / 捕获输出）；
  - **本地实现**（`LocalFS` / `LocalSubprocess`）；
  - 工具只依赖抽象——`harness/tools/` 里不再出现 `pathlib` 读写；
- **单槽服务**：同一能力重复注册 provider 直接报错（fail loud）；
  provider 选择在**显式 resolve 步骤**完成（不藏在执行函数里，呼应铁律 #6）；
- 至少两个新 provider：
  - `MemoryFS`（内存实现，测试用，不碰磁盘）；
  - `WorkspaceJailFS`（策略 provider：越出工作目录的写被拒，直接返回结构化错误）。

**验收**：
- [ ] **同一套工具测试在 `local` 与 `memory` 两个 provider 下都全绿**（换实现不改测试）
- [ ] jail 越权返回的错误结构与其它工具错误一致（同样是 `{"error": ...}`）
- [ ] 重复注册同一能力报错；provider 在显式 resolve 处选定
- [ ] 门禁全绿；基线 22 用例仍全过

**决策记录**：`docs/decisions/0007-capability-seams.md`（三角色边界怎么切、provider 何时选定）。

## 2) 运行方法

```bash
cd P2_Coding/_02_Capability_Seams

python -m pytest -q                          # 基线 + 本阶段测试
python -m pytest -q -k provider              # 双 provider 对照（若按此命名）
python demo.py                               # 端到端回归
python -m harness run "把 hello 写到 notes/a.txt" --fake
./run.bat chat --fake
```

配一个**双 provider 对照 demo**（本阶段建议新增）：同一句"写文件"指令，分别用
`local` 与 `memory` 跑，观察"结果一致、落盘行为不同"——这正是接缝要证明的事。

## 3) 完成后的去向

M5 的产物是"可替换的能力层"。`_03_Profile_Composition` 会从本阶段复制起点，把
"选哪个 provider"从代码搬到配置（profile）。
