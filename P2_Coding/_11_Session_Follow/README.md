# _11_Session_Follow —— session.follow（重放 + 订阅）+ attach

P2 第 11 阶段（对应学习计划 **M7**）。本阶段**只加一个机制**：

> `follow = 重放（日志）+ 订阅（实时）`；`attach` 客户端可断线重连并**补齐缺失事件**。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/server/ | _10 的 RPC | ★ session.follow + 实时订阅 |
| harness/cli.py | serve | 加 `attach` 客户端 |

## 0) 现状基线

```bash
cd P2_Coding/_11_Session_Follow
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/client/connection/`（流式跟随、重连）
- `docs/api-gateway.md`

**做**：
- `session.follow(from_seq)` = 先重放 `seq > from_seq` 的既有事件，再挂实时订阅（无缝衔接）；
- `attach` 客户端：流式渲染；断线后重连补齐。

**验收**：
- 两个终端 `serve` + `attach` 共享同一会话
- **kill 掉 attach 后重连能补齐断线期间缺失的事件**
- follow = 重放 + 订阅的 golden 测试
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0016.md`。

## 2) 运行方法

```bash
cd P2_Coding/_11_Session_Follow
python -m pytest -q
python -m harness attach --session s1 --from 10
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

P2（M4-M7）完成。之后可进入 **M8 选修**（子代理与委派、上下文压缩、审批与沙箱、Skill 与 MCP、Workflow/Ralph），每项沿用同一循环：定验收 -> 实现 -> 测试 -> 笔记。
