# _10_Rpc_Transport —— stdio JSON-RPC：initialize / session.prompt

P2 第 10 阶段（对应学习计划 **M7**）。本阶段**只加一个机制**：

> harness 成为**常驻服务**：stdio 换行 JSON-RPC，先做 `initialize` 握手与 `session.prompt`。

**状态：✅ 已完成（2026-10-09）** ｜ 决策记录 [docs/decisions/0015](../../docs/decisions/0015-rpc-transport.md)

## 本阶段自包含

本目录带一份完整的 `harness/` 副本（P2 约定：阶段之间不共享代码；起点 = `_09` 完成后的
全部顶层包），本阶段新增顶层 `server/` 与 `serve.py`。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——本阶段一行未改；
> 本阶段新增的机制在阶段主目录顶层，与 `harness/` 平级。

| 位置 | 角色 | 本阶段做了什么 |
|---|---|---|
| `server/protocol.py`（顶层） | ★ **本阶段新增** | 帧词汇：一行 ↔ 请求/通知；稳定错误码；帧构造 |
| `server/service.py`（顶层） | ★ **本阶段新增** | `HarnessService`：initialize 握手 + session.prompt（会话缓存/落盘） |
| `server/transport.py`（顶层） | ★ **本阶段新增** | `LineTransport`：换行收发循环；异常 → 稳定错误帧 |
| `serve.py`（顶层） | ★ 入口 | stdio 服务（banner 走 stderr，stdout 只出帧） |
| `harness/`（顶层） | M1–M3 基线库（冻结） | 未改动 |

## 0) 现状基线

```bash
cd P2_Coding/_10_Rpc_Transport
python -m pytest -q        # 22 个基线用例 + 40 个本阶段用例 = 62
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/sdk/protocol/src/transport.ts`（换行 JSON-RPC：id+method 请求 / 只有 id 响应 / 只有 method 通知）
- `packages/api/gateway/src/index.ts`（RPC 网关的调用与错误路径）

**做**：
- ✅ 换行 JSON-RPC 收发；`initialize` 握手；`session.prompt` 跑一个 turn；
- ✅ 协议 **golden 测试**（请求/响应报文的稳定快照）。

**验收**：
- [x] 换行 JSON-RPC 收发稳定（批量子进程往返 + 混合帧序列）
- [x] `initialize` 握手（含能力清单；握手前门禁 -32002；重复握手幂等）
- [x] `session.prompt` 落盘（响应与 JSONL 双向验证；同名会话 turn 接续）
- [x] golden 测试通过（帧构造 5 条 + 端到端 3 条，写死在测试里）
- [x] 基线 22 用例仍全绿；门禁绿（62 用例，ruff 通过）

## 2) 本阶段新增的东西

**三层单向依赖**（服务不碰字节流、传输不碰业务——测试因此能用 StringIO 驱动
与 stdio 完全同构的路径）：

```
serve.py            stdio 入口（banner→stderr；stdout 只出帧）
    └─ LineTransport   换行收发：一行进 → parse → dispatch → 一行出
         └─ HarnessService   initialize / session.prompt（会话缓存 + 落盘）
              └─ server/protocol   帧词汇与错误码（golden 就钉在这层）
```

**协议**：每行一帧 JSON-RPC 2.0；四类帧按字段区分（请求 / 响应 / 错误 / 通知）。
错误码：标准段 `-32700 / -32600 / -32601 / -32602 / -32603` +
server 段 `-32002 NOT_INITIALIZED`（未握手）。**id 的尽力归属**：帧能解析出 id
但不合法时尽量带回 id（客户端可配对）；纯解析失败按规范回 `id: null`。

**服务**：两个方法——`initialize`（返回协议版本 / 服务器标识 / 能力清单：
方法列表 + 真实工具面）；`session.prompt`（`{session?, text}` → turn 摘要）。
会话按 id **缓存**（常驻服务里同名继续同一场，turn 接续）；落盘走 `_01`–`_09`
的现成装配（打开即恢复的日志语义兜底：进程重启后重连同名会话，历史照样回来）。

**两条服务端纪律**：

1. **握手门禁**：`initialize` 之前调用其它方法 → `-32002`（"谁在连、能不能用"
   变成协议里可检查的一步，不再靠人盯终端）。
2. **审批 fail-closed**：服务端没有人能点 y/n——默认 `AutoDeny`（拒绝一切需审批
   操作），要放开必须显式 `--auto-approve`。默认安全，显式放开。

## 3) 运行方法

```bash
cd P2_Coding/_10_Rpc_Transport
python -m pytest -q

# ★ 招牌入口：stdio JSON-RPC 服务（stdin 收帧、stdout 出帧、EOF 退出）
python serve.py                       # dev profile（离线：FakeLLM + MemoryFS）
python serve.py --profile prod        # prod profile（DeepSeek + 本地）
python serve.py --auto-approve        # 放开审批（默认 fail-closed）

# 手工试跑（Git Bash）：一行一帧
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n' \
     '{"jsonrpc":"2.0","id":2,"method":"session.prompt","params":{"session":"s1","text":"帮我算 2+3"}}\n' \
  | python serve.py

./run.bat serve            # 启动器也路由了 serve
python demo.py             # 离线演示：0a 帧词汇 / 0b 服务交互（握手·接续·错误·审批）/ 0c 配置底座回归
```

**服务化长什么样**：stdout 里只有一行行 JSON 帧（握手响应、turn 结果、错误帧），
banner 在人眼看的 stderr 里；同一会话连发两条 prompt，第二条的 `turn` 接在第一条后面；
拒绝审批时服务不炸——模型收到结构化拒绝、照常给出最终回答。

## 4) 完成后的去向

`_11_Session_Follow` 加**事件流跟随**：`session.follow(from_seq)` = 重放（日志）+
订阅（实时），客户端 `attach` 断线后从 `last_seq` 续订补齐——P2 的最后一个阶段，
也是 M7 的收官（服务从"请求-响应"升级为"可跟随的事件流"）。
