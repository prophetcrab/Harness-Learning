# _03_Profile_Composition —— M6：Profile 式组装

P2 第三个阶段，对应学习计划 **M6**。目标：**能力组合从代码变成配置数据**；
一行 patch 完成 provider 替换。

## 本阶段是自包含的

本目录带一份**完整的 `harness/` 副本**。起点应当是 `_02`（能力接缝）完成后的代码——
把它的 `harness/` 整体复制过来，再加上 `profiles/` 目录。P2 的约定：阶段之间不共享代码。

`harness/` 里与本阶段相关的部分：

| 子包/文件 | 现状 | 本阶段要做的 |
|---|---|---|
| `harness/config/` | 占位（只有 `__init__.py`） | ★ 插件协议 + profile 加载 + patch 叠加 |
| `harness/providers/` | `_02` 的成果：`FileSystem`/`Subprocess` 接缝 | provider 选择改由 profile 决定 |
| `harness/llm/` | FakeLLM / DeepSeekProvider 二选一（代码里） | 改由 profile 决定 |
| `harness/cli.py` | 直接构造 provider / registry | 改为 `--profile` 驱动装配 |
| `profiles/`（新建） | — | `base.yaml` + `dev.yaml` + `prod.yaml` |

## 0) 现状基线（动手前先确认它绿的）

```bash
cd P2_Coding/_03_Profile_Composition
python -m pytest -q        # 22 个用例，必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/boot/app-boot/src/profile.ts`（profile 结构）
- `packages/bundle/base/cordis.patch.yml`（共享底座补丁长什么样）
- `apps/cli/src/profile-boot.ts`（补丁叠加顺序）
- `docs/cordis-primer.md`（loader configuration 节）

**做**：
- `harness/config/`：
  - 极简**插件协议**：`Plugin.setup(ctx) -> disposer`；**注册即 effect**
    （disposer 在卸载时自动回卷，呼应铁律 #3）；
  - `profiles/*.yaml`：**有序层 + 按 id patch**（替换整块 config 或 insert 新行）；
    叠加顺序：`base → profile patch → 用户 patch → CLI --patch`；
- `python -m harness --profile <name> dump-config`：打印最终装配出的可读配置树；
- 两个 profile：
  - `dev`：FakeLLM + MemoryFS（离线、可重复）；
  - `prod`：DeepSeekProvider + 本地 FS。

**验收**：
- [ ] 同一个 base，两个 profile 产出**不同且可读**的配置树
- [ ] **换 LLM provider 只改一行**（base 只加一行 patch）
- [ ] 故意写错配置 → 启动即报错并**指出位置**（fail loud，铁律 #8）
- [ ] `dump-config` 能看到每个生效配置项来自哪一层
- [ ] 门禁全绿；基线 22 用例仍全过

**决策记录**：`docs/decisions/0008-profile-composition.md`（为什么用分层 patch 而非
单文件配置；叠加顺序为什么是这个次序）。

## 2) 运行方法

```bash
cd P2_Coding/_03_Profile_Composition

python -m pytest -q
python -m harness --profile dev dump-config     # 看 dev 装配树
python -m harness --profile prod dump-config    # 看 prod 装配树
python -m harness --profile dev run "帮我算 2+3"  # 用 dev profile 跑
python demo.py
./run.bat --profile dev dump-config
```

建议做一个**对照实验**：只改 `dev.yaml` 里 LLM provider 那一行（FakeLLM → DeepSeek），
重新 `dump-config` 看差异——这就是"换 provider 换产品"的可配置版本。

## 3) 完成后的去向

M6 的产物是"配置驱动的装配"。`_04_Service` 会从本阶段复制起点，把 harness 变成常驻服务。
