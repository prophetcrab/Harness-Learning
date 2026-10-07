# _07_Plugin_Effect —— 插件协议与 effect（ctx + disposer）

P2 第 7 阶段（对应学习计划 **M6**）。本阶段**只加一个机制**：

> 极简插件协议 `Plugin.setup(ctx) -> disposer`；**注册即 effect**，卸载时自动回卷。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/config/ | 占位（只有 __init__.py） | ★ Context + Plugin 协议 + effect 回卷 |
| harness/mini.py | 手动构造各部件 | 改为经 `ctx` 注册（注册即 effect） |

## 0) 现状基线

```bash
cd P2_Coding/_07_Plugin_Effect
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `docs/cordis-primer.md`（ctx 与 effect 一节）
- `packages/boot/app-boot/src/profile.ts`（插件视角）

**做**：
- `Context`（插件共享的注册中心）；
- `Plugin.setup(ctx) -> disposer`；注册即 effect，卸载时 disposer **自动回卷**（铁律 #3）；
- 重复注册同一槽位直接报错。

**验收**：
- setup 返回 disposer
- 卸载后注册物**自动撤销**（回卷）
- 重复注册报错
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0012.md`。

## 2) 运行方法

```bash
cd P2_Coding/_07_Plugin_Effect
python -m pytest -q
python -m harness run "帮我算 2+3" --fake
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

`_08_Profile_Layers` 把「装配」写成配置层。
