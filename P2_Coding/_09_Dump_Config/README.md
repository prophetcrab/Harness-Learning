# _09_Dump_Config —— dump-config 与配置错误定位

P2 第 9 阶段（对应学习计划 **M6**）。本阶段**只加一个机制**：

> `--profile <name> dump-config` 打印最终配置树与**每项来源**；配错启动即报错并**指出位置**。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/config/ | _08 的分层加载 | ★ dump-config + 配置校验 |
| harness/cli.py | --profile | 加 `dump-config` 子命令 |

## 0) 现状基线

```bash
cd P2_Coding/_09_Dump_Config
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `docs/cordis-primer.md`（loader configuration 节）
- `apps/cli/src/profile-boot.ts`

**做**：
- `dump-config`：输出可读配置树 + 每项来自哪一层；
- 配置校验：未知键 / 类型错 / 引用缺失 -> 启动即报错并**指出位置**（铁律 #8 fail loud）。

**验收**：
- dump-config 可读、显示来源层
- 故意写错配置 -> 启动即报错并指出位置
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0014.md`。

## 2) 运行方法

```bash
cd P2_Coding/_09_Dump_Config
python -m pytest -q
python -m harness --profile dev dump-config
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

M6 完成。下一阶段 `_10_Rpc_Transport` 进入 M7 服务化。
