# _08_Profile_Layers —— profiles YAML 分层与 patch

P2 第 8 阶段（对应学习计划 **M6**）。本阶段**只加一个机制**：

> 能力组合从代码变成**配置数据**：`profiles/*.yaml` 有序层 + 按 id patch；**一行 patch 换 provider**。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/config/ | _07 的 ctx + 插件协议 | ★ profile 加载 + 分层 patch |
| profiles/（新建） | — | base.yaml + dev.yaml + prod.yaml |
| harness/cli.py | 直接装配 | 加 `--profile` |

## 0) 现状基线

```bash
cd P2_Coding/_08_Profile_Layers
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/bundle/base/cordis.patch.yml`（底座补丁长什么样）
- `apps/cli/src/profile-boot.ts`（补丁叠加顺序）

**做**：
- `profiles/*.yaml`：**有序层 + 按 id patch**（替换整块 config 或 insert 新行）；
- 叠加顺序：`base -> profile patch -> 用户 patch -> CLI --patch`；
- 两个 profile：`dev`（FakeLLM + MemoryFS）、`prod`（DeepSeek + 本地）。

**验收**：
- 同 base、两 profile 产出**不同且可读**的树
- 换 LLM provider **只改一行**
- 叠加顺序正确
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0013.md`。

## 2) 运行方法

```bash
cd P2_Coding/_08_Profile_Layers
python -m pytest -q
python -m harness --profile dev run "帮我算 2+3"
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

`_09_Dump_Config` 让最终配置**可见**、错误**可定位**。
