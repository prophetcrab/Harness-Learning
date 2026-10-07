# _04_Service —— M7：服务化与多前端

P2 最后一个阶段，对应学习计划 **M7**。目标：**harness 成为常驻服务**；
前端通过事件流跟随会话（断线可补）。

## 本阶段是自包含的

本目录带一份**完整的 `harness/` 副本**。起点应当是 `_03`（Profile 组装）完成后的代码。
P2 的约定：阶段之间不共享代码，每个阶段都能单独跑、单独读。

`harness/` 里与本阶段相关的部分：

| 子包/文件 | 现状 | 本阶段要做的 |
|---|---|---|
| `harness/server/` | 占位（只有 `__init__.py`） | ★ 写 JSON-RPC 服务 + 事件流 follow |
| `harness/session/` | 事件日志 + 投影 + `replay` 能力 | `session.follow(from_seq) = 重放 + 订阅` |
| `harness/webui/server.py` | 本地可视化页（单进程、每请求 open） | 可改为 attach 到常驻服务的客户端 |
| `harness/cli.py` | `chat/run/list/show/fork` | 增加 `serve` / `attach` 子命令 |

## 0) 现状基线（动手前先确认它绿的）

```bash
cd P2_Coding/_04_Service
python -m pytest -q        # 22 个用例，必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/sdk/protocol/src/transport.ts`（换行 JSON-RPC 传输）
- `packages/api/gateway/src/index.ts`（RPC 网关）
- `packages/host/webserver/src/index.ts`
- `packages/client/connection/`（流式跟随、重连）
- `docs/api-gateway.md`

**做**：
- `harness/server/`：`python -m harness serve`
  - 先做 **stdio 换行 JSON-RPC**：`initialize` / `session.prompt` / `session.follow`；
  - 可选升级 HTTP + WebSocket；
- **`session.follow(from_seq)` = 重放（日志）+ 订阅（实时）**——这是本阶段的核心等式：
  先重放 `seq > from_seq` 的既有事件，再挂上实时订阅，两者无缝衔接；
- 客户端 `python -m harness attach`：流式渲染（`rich` 可选，无则纯文本）。

**验收**：
- [ ] 两个终端：一个 `serve`，一个 `attach`，共享同一会话
- [ ] **kill 掉 attach 后重连，能补齐断线期间缺失的事件**（靠 follow 的重放部分）
- [ ] 协议 **golden 测试**（请求/响应报文的稳定快照）
- [ ] 门禁全绿；基线 22 用例仍全过

**决策记录**：`docs/decisions/0009-follow-equals-replay-plus-subscribe.md`
（为什么 follow 拆成"重放 + 订阅"，以及断线补齐如何在两者之间无缝）。

## 2) 运行方法

```bash
cd P2_Coding/_04_Service

python -m pytest -q
python -m harness serve                     # 启动常驻服务（stdio JSON-RPC）
python -m harness attach --session s1       # 另一个终端：接入并跟随
python -m harness attach --session s1 --from 10   # 从 seq=10 补齐
python demo.py
./run.bat serve
```

**断线补齐实验**（本阶段的招牌演示）：
1. 终端 A：`serve`；终端 B：`attach`，发几条消息；
2. 直接 kill 掉终端 B 的 attach（模拟断线）；
3. 期间在终端 C 或用 API 再发消息，让会话继续增长；
4. 终端 B 重新 `attach --from <断点 seq>` → 应看到断线期间的事件被**补全**。

这就是"事件溯源 + follow"能提供、而普通流式接口给不了的东西。

## 3) 完成后的去向

M7 完成即走完学习计划的 M4–M7 主线。之后可进入 **M8 选修**（子代理与委派、上下文压缩、
审批与沙箱、Skill 与 MCP、Workflow/Ralph），每项沿用同一循环：定验收 → 实现 → 测试 → 笔记。
