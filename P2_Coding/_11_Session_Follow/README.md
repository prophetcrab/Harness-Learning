# _11_Session_Follow —— session.follow（重放 + 订阅）+ attach

P2 第 11 阶段（对应学习计划 **M7**，**P2 收官**）。本阶段加两样东西：

> 1. `follow = 重放（日志）+ 订阅（实时）`；`attach` 客户端断线重连并**补齐缺失事件**。
> 2. **编码工具补齐**：`edit_file` / `search_text` / `find_files`——把 harness 补成一个
>    基本可用的 coding agent（骑在既有 FileSystem 接缝上，不引入新机制）。

**状态：✅ 已完成（2026-10-09）** ｜ 决策记录 [0016](../../docs/decisions/0016-session-follow.md)（follow）、[0017](../../docs/decisions/0017-coding-tools.md)（工具）

## 本阶段自包含

起点 = `_10` 完成后的全部顶层包（`harness/` + `prompt/` + `context/` + `providers/` +
`kernel/` + `config/` + `profiles/` + `server/`），本阶段扩展 `providers/toolbox.py`、
`server/`（订阅 + TCP）、新增 `attach.py`。

> **代码组织约定（P2）**：`harness/` 是**冻结的 M1–M3 基线库**——P2 全程一行未改。

| 位置 | 本阶段做了什么 |
|---|---|
| `server/service.py` | ★ `session.follow`（重放 + 订阅）；事件产生即推（on_event → flush）；多连接订阅台账 |
| `server/transport.py` | ★ dispatch 接 `send` 回调（服务端主动推帧）；写锁；断开时注销订阅 |
| `server/tcp.py` | ★ 新增：多客户端 TCP 服务（每连接一线程，共享同一 service） |
| `providers/toolbox.py` | ★ 新增 edit_file / search_text / find_files（编码能力面） |
| `serve.py` | ★ `--listen` 支持 TCP 模式 |
| `attach.py` | ★ 新增：跟随客户端（流式渲染 / 发送 / 断线重连补齐） |

## 0) 现状基线

```bash
cd P2_Coding/_11_Session_Follow
python -m pytest -q        # 22 个基线用例 + 37 个本阶段用例 = 59
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/client/connection/`（流式跟随、重连）
- `docs/api-gateway.md`

**做**：
- ✅ `session.follow(from_seq)` = 先重放 `seq > from_seq` 的既有事件，再挂实时订阅（无缝衔接）；
- ✅ `attach` 客户端：流式渲染；断线后重连补齐；
- ✅ 编码工具：`edit_file`（精确替换）/ `search_text`（内容搜索）/ `find_files`（按名查找）。

**验收**：
- [x] 两个客户端 `serve` + `attach` 共享同一会话（TCP 多连接端到端测试）
- [x] **断线后重连能补齐断线期间缺失的事件**（`test_reconnect_fills_gap_exactly`：
      断开 → 期间产生事件 → from=已见 seq 重连 → 恰好补齐、不重不漏）
- [x] follow = 重放 + 订阅的 golden 测试（帧形状 / 帧序 / from_seq 过滤语义）
- [x] 编码工具在 local / memory / jail 下语义一致；edit 拒歧义；检索有上限
- [x] 真实 API 实跑：模型用 search + edit 完成任务、磁盘文件确实被改
- [x] 基线 22 用例仍全绿；门禁绿（59 用例，ruff 通过）

## 2) 本阶段新增的东西

**2a) `session.follow`：重放 + 订阅**（`server/service.py`）

```
客户端                               服务端
  │ session.follow(from_seq=7)        │
  │ ─────────────────────────────────►│
  │ ◄─ session.event {seq: 8}  重放   │  日志里 seq>7 的既有事件逐条推
  │ ◄─ session.event {seq: 9}         │
  │ ◄─ …                              │
  │ ◄─ result {replayed, latest_seq}  │  重放结束点 = 订阅起点（无缝衔接）
  │                                   │
  │ ◄─ session.event {seq: 10} 实时   │  此后该会话的每条新事件都推
```

- **断线补齐不需要任何缓存机制**：断线期间的事件本来就在日志里（铁律 #1），
  重连时按 seq 重放即可——"follow = 重放 + 订阅"就是这个意思；
- **事件产生即推**：打开会话时挂 `on_event` → 每条会话事件记录后 flush，
  长 turn 进行中也能实时看到（不是等 turn 结束批量给）；
- **多连接**：订阅记录各自的 sender；A 连接产生的会话事件会推给 B 连接的订阅者
  （`test_two_clients_share_one_session`）；连接断开由 transport 的 on_close
  注销订阅（死订阅不会留在服务里）。

**2b) TCP 传输 + attach 客户端**

- `server/tcp.py`：`ThreadingTCPServer`，每连接一线程、复用同一条
  `LineTransport` 收发路径（与 stdio 完全同构）；`serve.py --listen 端口` 启动；
- `attach.py`：连上后 `initialize` + `session.follow(from_seq=--from)`，
  事件流式渲染成人话（turn 边界 / 消息 / 工具结果），提示符可继续发消息；
  **断线自动重连**——重连时用"已见最大 seq"作为 `from_seq`，缺口由服务端重放补齐。
  命令：`/seq` 看当前断点；`/exit` 退出。

**2c) 编码工具三件**（`providers/toolbox.py`，`_11` 把工具面补成基本 coding agent）

| 工具 | 作用 | 关键语义 |
|---|---|---|
| `edit_file` | 局部精确替换 | old **恰好出现一次**才改；0 次/多次分别报 `FS_EDIT_NO_MATCH` / `FS_EDIT_AMBIGUOUS`（拒绝猜）——防误改 |
| `search_text` | 内容正则搜索 | 返回 文件+行号+该行；结果有上限（截断时如实说明）；坏正则报 `INVALID_PATTERN` |
| `find_files` | 按名查找 | shell 通配（`*.py`、`test_*`）；结果有上限 |

三者都**骑在既有接缝上**（FileSystem 的 edit/list/read），因此：
local / memory / jail 下行为一致；jail 的围栏对 `edit_file` 同样生效（越界改动被拒），
而 `search_text` / `find_files` 是只读的、不受围栏影响（策略只拦改动）。
`edit_file` 与 `write_file` 一样需要审批。

工具面（dev/profile 全装配）：`calculate` · `read_file` · `write_file` · `list_files` ·
**`edit_file`** · **`search_text`** · **`find_files`** · `shell`（+ 可选 `web_search`）。

## 3) 运行方法

```bash
cd P2_Coding/_11_Session_Follow
python -m pytest -q

# —— 跟随（stdio：attach 自己拉起 serve.py 子进程）——
python attach.py --session s1                     # 跟随（重放+实时），提示符可发消息
python attach.py --profile prod --session s1      # 真实 API
python attach.py --session s1 --ask "你好"         # 非交互：发一条后退出

# —— 多客户端（TCP：两个终端共享同一会话）——
# 终端 1：起服务
python serve.py --profile prod --listen 8765
# 终端 2/3：分别跟随同一会话（一个发消息，另一个实时看到事件）
python attach.py --connect 8765 --session s1
python attach.py --connect 8765 --session s1 --verbose

# —— 断线补齐的手工验证 ——
# 1) attach 跟随后 Ctrl+C（或 /exit）；2) 另一个 attach 发几条消息；
# 3) 重连：attach.py --session s1 --from <上次的断点>   → 缺口被重放补齐

# —— 编码工具实战（真实 API）——
python chat.py --profile prod --ask "search_text 找出所有 TODO，并用 edit_file 把 helper.py 里的一个改掉" --no-approve
python demo.py    # 离线演示：0a follow 帧序/断线补齐；0b 新工具；0c 配置底座回归
```

## 4) 完成后的去向

**M7 完成 → P2（M4–M7）全部完成。** 十一阶段总览见 [P2_Coding/README.md](../P2_Coding/README.md)；
项目后续路线（P3 起：垂类化 / 插件内核重构 / SDK 分发 / 回 dsh 写插件）见根
[README.md](../README.md) 的"未来路线图"。
