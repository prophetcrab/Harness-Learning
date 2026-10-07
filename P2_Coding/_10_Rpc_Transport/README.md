# _10_Rpc_Transport —— stdio JSON-RPC：initialize / session.prompt

P2 第 10 阶段（对应学习计划 **M7**）。本阶段**只加一个机制**：

> harness 成为**常驻服务**：stdio 换行 JSON-RPC，先做 `initialize` 握手与 `session.prompt`。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/server/ | 占位（只有 __init__.py） | ★ JSON-RPC 传输 + initialize / session.prompt |
| harness/cli.py | chat / run / list / show / fork | 加 `serve` 子命令 |

## 0) 现状基线

```bash
cd P2_Coding/_10_Rpc_Transport
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/sdk/protocol/src/transport.ts`（换行 JSON-RPC）
- `packages/api/gateway/src/index.ts`（RPC 网关）

**做**：
- 换行 JSON-RPC 收发；`initialize` 握手；`session.prompt` 跑一个 turn；
- 协议 **golden 测试**（请求/响应报文的稳定快照）。

**验收**：
- 换行 JSON-RPC 收发稳定
- `initialize` 握手
- `session.prompt` 落盘
- golden 测试通过
- 基线 22 用例仍全绿

**决策记录**：`docs/decisions/0015.md`。

## 2) 运行方法

```bash
cd P2_Coding/_10_Rpc_Transport
python -m pytest -q
python -m harness serve
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

`_11_Session_Follow` 加事件流跟随与**断线补齐**。
