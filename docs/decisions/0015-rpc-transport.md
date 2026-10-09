# 0015 stdio JSON-RPC：服务化第一刀，握手门禁与 fail-closed

- 日期：2026-10-09
- 阶段：P2_Coding/_10_Rpc_Transport（学习计划 M7 第一步）
- 状态：已采纳

## 背景

M1–M6 结束时，mini-harness 是一个**程序**：`python chat.py` / `demo.py` 在同一个
进程里跑完整条链路。M7 的目标是"harness 成为常驻服务；前端通过事件流跟随会话"——
第一步是**给它一个能被别的进程连接、调用、并获取结果的协议**。

参考实现 dsh 的对应物是 `packages/sdk/protocol/src/transport.ts`（换行 JSON-RPC 2.0）
与 `packages/api/gateway`（RPC 网关）。本阶段按 README 取最小切片：stdio 换行
JSON-RPC、`initialize` 握手、`session.prompt` 跑 turn、golden 报文快照。

## 决策

**1) 传输选"换行 JSON-RPC 2.0"**：一行一帧、按字段区分四类帧（id+method=请求、
只有 id=响应、只有 method=通知、error 字段=错误响应）——dsh transport.ts 的定义，
也是 JSON-RPC 标准的惯用简化。为什么不直接上 HTTP/WebSocket：stdio 是"被另一个
进程当子进程拉起来"的最简形态（编辑器插件、脚本、测试都能驱动），无端口无网络，
与学习计划的第二步（`_11` 的 follow/attach）正好衔接；HTTP/WS 属于产品化升级，
计划里也写着"可选"。

**2) 三层单向依赖**（protocol → transport → service，serve.py 做入口）：
- **protocol** 只管"一行文本 ↔ 一种帧 + 稳定错误码 + 帧构造"；
- **transport** 只管收发循环与异常翻译（任何异常 → 稳定形状的错误帧，进程永不被
  单条坏帧炸掉）；它只认识 `dispatch(method, params)` 一个方法；
- **service** 只管业务（握手、prompt），不碰字节流。

收益：**测试能用 StringIO 驱动与 stdio 完全同构的收发路径**（unit/wiring/golden
全跑在 StringIO 上），而真实 stdio 只多一个子进程测试兜底。这一刀切得干净，
`_11` 加 `session.follow` 时只需在 service 加方法 + transport 加"服务端主动推帧"
的出口。

**3) 握手门禁（-32002）与幂等**：`initialize` 之前调用其它方法 → `NOT_INITIALIZED`。
理由：服务化之后"谁在连、版本合不合、有没有我要的能力"应当成为协议里**可检查的
一步**；能力清单（方法列表 + 真实工具面）放进 initialize 响应，客户端可以在
调用前就知道这个服务能干什么。重复 initialize 幂等返回（把"再握一次手"当作
良性重试而不是错误——网络/客户端重试语义友好）。

**4) 会话缓存 + 底层日志兜底**：常驻服务里，同名 `session.prompt` 要接在上一句后面
（turn 接续），于是缓存 `session id → ContextHarness`。**但正确性不依赖缓存**：
底层是"打开即恢复"的日志语义——进程重启、缓存清空后重连同名会话，历史照样从
JSONL 投影回来（`test_same_session_continues_turn_number` 与子进程测试分别验证
缓存内与跨进程两条路径）。缓存只是省一次磁盘读。

**5) 审批 fail-closed（服务端默认安全）**：CLI 里审批可以弹给人看（`PromptApprover`），
服务端没有"人"——默认 `AutoDeny`（拒绝一切需审批操作），放开必须显式
`--auto-approve`。这与 `_03` 定的审批纪律一脉相承（问不到应答方就拒绝），
但服务化让它从"兜底"升级为"默认档"：远程/无人值守场景下，默认放行才是事故源。

**6) golden 测试写死不自动生成**：快照常量直接写在测试文件里（帧构造 5 条 +
端到端 3 条）。不用"首次运行自动生成快照"的框架——人为审核过的报文才是契约；
协议无意中改形状时必须**测试变红**，而不是被静默更新。端到端 golden 能成立
是因为 dev profile 全确定（FakeLLM 剧本固定、MemoryFS 无外部状态、响应里
没有时间与随机数）。

**7) stdout 是协议线，banner 走 stderr**：stdio 服务的经典纪律——客户端把 stdout
每一行都当帧解析，任何人话混进去都是解析错误。`serve.py` 遵守，子进程测试
断言"stdout 只有帧、`[serve]` 前缀在 stderr"。

## 备选与排除理由

1. **HTTP + WebSocket 起步**。排除：端口/网络/并发会给"第一步"叠三个新问题；
   stdio 子进程形态覆盖了"被程序调用"的核心场景，且与 `_11` 的断线重连语义
   （follow from_seq）天然兼容。计划里 HTTP/WS 是"可选升级"。
2. **自定义协议（不做 JSON-RPC）**。排除：JSON-RPC 的错误码/配对 id/通知三件套
   是现成的、被广泛理解的规范；dsh 的传输层就是它。自造协议省不下什么，还丢了
   与生态工具的互操作性。
3. **不做握手门禁（任何方法随时可调）**。排除：服务化后"未初始化就调用"是最常见的
   客户端错误；-32002 让它变成一条明确的、可自解释的报文，而不是一堆
   "会话不存在"之类的下游误报。
4. **握手成功前不回能力清单**。排除：清单（方法 + 工具面）是客户端"探活 + 决定
   怎么用"的唯一依据；没有它客户端只能靠试错。
5. **审批默认放行（AutoApprove）**。排除：服务端无人值守，"默认放行"意味着任何
   连上来的客户端都能让模型自由写文件/执行命令——默认必须是拒绝，放开要显式。
6. **一次性会话（每次 prompt 都新开会话）**。排除：那就丢掉了"常驻服务"相对
   一次性 CLI 的核心价值（多轮接续）；且与日志的"打开即恢复"语义冲突。

## 后果

- 约束：`server/` 依赖 `context/kernel/providers/harness` 的既有装配（服务底座
  就是 `_07`–`_09` 的配置 boot）；协议层不依赖任何业务。错误码一旦发布即契约
  （测试钉住 -32002 的取值）；帧形状由 golden 钉住。
- 收益：**harness 第一次能被"当服务用"**——批量子进程往返稳定（测试里用
  `subprocess` 驱动 `serve.py`，断言 stdout 纯帧、日志落盘）；握手/会话接续/
  三类错误码/审批 fail-closed 全部有测试与 demo 演示；服务化没有新造装配——
  它直接坐在 `_07`（effect 台账）、`_08`/`_09`（配置装配）之上。62 个用例
  （22 基线 + 40 新增），全项目 719 用例全绿；跨 `_01`–`_10` 同进程 603 用例无串味。
- 债务与后续：
  1. **只做请求-响应**：事件流跟随（`session.follow`）是 `_11` 的题目——transport
     目前没有"服务端主动推帧"的出口，届时补（协议层已预留 notification 帧）。
  2. **单连接串行**：stdio 形态天然单客户端、帧串行处理；并发是 HTTP 时代的事。
  3. **取消/超时未做**：长 turn 期间客户端无法打断（JSON-RPC 的 notification
     可以承载 cancel，留待需要时）。
  4. **`python -m harness serve` 未接线**（沿袭：`harness/` 冻结；入口为阶段
     顶层 `serve.py`）。归档时的"统一入口"是 P3+ 的话题。
