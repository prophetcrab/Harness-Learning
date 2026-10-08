# 0009 FileSystem 接缝：三角色分离，机制与策略分层

- 日期：2026-10-08
- 阶段：P2_Coding/_04_Filesystem_Seam（学习计划 M5 第一步）
- 状态：已采纳

## 背景

M1–M4 的文件工具"能用"，但实现是直接操作 pathlib 的闭包（`harness/tools/workspace.py`
里的 `make_file_tools`）：读写函数拿到一个 `root: Path`，然后 `target.write_text(...)`。
三个问题：

1. **换不了**：想让文件写到内存（测试不碰磁盘）、或换到别处，只能改工具代码；
2. **测不了干净**：工具测试要么忍受真实磁盘副作用，要么另写一份假实现——
   "同一套测试跑两个实现"没有结构支撑；
3. **策略没有落脚点**：`_05` 要加的"越界拒绝"逻辑，在现在的结构里只能塞进
   工具函数——机制（怎么读写）与策略（允许读写什么）糊在一起。

M5 的目标是"能力接缝：Definition + Provider + Consumer 三角色，可整体替换"，
`fs/fs`（dsh）就是这个模式的教科书样例：`ctx.fs` 定接口，`fs-local` / `fs-sandbox`
各自实现，工具包 `tool-fs` 只调接口。

## 决策

**1) 接缝定义做"纯机制"**（`providers/filesystem.py`）：`FileSystem` 协议四个方法——
`read_text` / `write_text` / `edit_text` / `list_files`；配套 `FsError(code, message)`
与五个稳定错误码（NOT_FOUND / NOT_A_FILE / NOT_A_DIR / EDIT_NO_MATCH / EDIT_AMBIGUOUS）。
协议里**没有任何策略**：不设根目录白名单、不拒绝路径——那些是"允许做什么"，
属于策略层。这样定义才可能同时容下"真实磁盘、内存、沙箱"三类实现。

**2) 两个实现故意选"性格相反"的**（`local.py` / `memory.py`）：LocalFS 贴真实磁盘
（`root/path` 解析、父目录 mkdir、`rglob` 递归列举）；MemoryFS 只有一本字典
（`规范化路径 → 文本`，目录隐式推导）。选这两个不只是"多写一个类"——它们证明
协议抽象到位：一个碰世界、一个不碰世界，却在同一套断言下行为一致。

**3) 错误用稳定码，工具层翻译**（铁律 #7 的落地）：provider 抛 `FsError`，
**消费者**（`toolbox.py`）把它转成 `{"error": 人话, "code": 稳定码}` 回给模型。
分工明确：provider 说"发生了什么"（机器可判读的码 + 人话），工具层说"怎么讲给模型听"。
调用方按 code 分支而不是比对消息文本（dsh 的 `FsError` 同样承诺 stable code）。

**4) 单槽服务 + 显式解析**（铁律 #5 / #6，`service.py`）：`ServiceContainer`
一个能力名只容一个 provider，重复 register 直接报错；`resolve` 是唯一的取用处，
未注册也报错。于是"这次用哪个实现"永远是**代码里一行明确的 resolve**，
不是散落在工具函数里的隐式默认。

**5) 消费者重写为只认协议**（`toolbox.py`）：`build_filesystem_registry(fs)`
接一份 FileSystem，把 read/write/list 三个工具实现建立在它上面；工具的定义
（名字/说明/schema/审批标记）从基线原样复用——**工具面对模型完全不变**，
变的只是背后的执行体。`edit_text` 本阶段只进协议与测试（没有 `edit` 工具），
工具面保持与基线一致是刻意的收敛。

**6) `edit` 语义一次做对**：恰好匹配一次才替换；0 次 / 多次分别报
`FS_EDIT_NO_MATCH` / `FS_EDIT_AMBIGUOUS`。多次匹配拒绝猜测是**原子编辑**的底线
（dsh 的 `editText` 同样承诺 literal + atomic）；它同时是"错误词表"的教学样本——
两种失败各有稳定码、各有人话。

## 备选与排除理由

1. **保留 pathlib 直写的工具，另外加一个 FileSystem 类做展示**。排除：那就是
   "文档式接缝"——工具没换实现，接缝没被证明。本阶段的验收恰恰是"同一套测试
   跑两个 provider 全绿"，只有真替换才算数。
2. **把根目录/越界检查放进 FileSystem 定义**。排除：那会把策略烧进所有实现，
   `_05` 的 WorkspaceJailFS 就没东西可加（只能重复）；且 LocalFS 从此不能用于
   "故意越界"的底层测试。机制归机制、策略归策略，是 dsh 把 fs 与 fs-sandbox
   分成两个包的同一条理由。
3. **错误只传消息文本、不加错误码**。排除：消息是给人读的，会随文案漂移；
   "哪个错误该重试、哪个该上报"这种程序判断必须靠稳定码。两边分开是
   dsh `FsError` 的形状，也让工具层能机械地翻译。
4. **ServiceContainer 允许多槽（同能力存列表）**。排除：一个会话里同时有多个
   "当前文件系统"会让"到底在用哪个"变得隐蔽；单槽 + 显式 resolve 才与
   铁律 #6 一致。要换就在 resolve 之前换（未来 `_07` 的插件 effect 会给
   "卸载回卷"一个正式机制）。
5. **MemoryFS 用真实临时目录冒充（tempfile）**。排除：那还是磁盘；本实现要的是
   "进程结束即消失、磁盘零痕迹"这个强性质（真实 API 演示里验证过），
   以及"测工具不碰磁盘"的测试卫生。

## 后果

- 约束：`providers/` 只依赖 harness（registry/schema/工具基座），不依赖 prompt/context；
  工具面的"名字/说明/schema"必须与基线保持兼容（本阶段用同一个参数模型保证）。
  `open_context_harness` 多了一个可缺省的 `tool_registry` 参数——向后兼容。
- 收益：**"同一套工具测试在两个 provider 下全绿"成为可执行的事实**（unit 与 wiring
  各有参数化套件，76 用例）；工具结果在两个 provider 下**逐字段相同**（demo 0c 与
  wiring 断言）；结构化错误进入模型视野（工具结果 + 日志）。`_05` 的策略 provider
  只需再实现协议一次，**上层一个字不改**。
- 债务与后续：
  1. **`edit_text` 并发语义未做**：读取与写回之间没有版本守卫（dsh 有 optional
     version guard）。单进程学习场景够用；将来做并发会话时再补"读时版本 → 写时校验"。
  2. **FileSystem 尚未成为"全局注册能力"**：现在 ServiceContainer 由入口按需构造
     （显式、局部）；`_07_Plugin_Effect` 落地后，能力注册将变成可卸载的 effect，
     由插件系统统一装载——届时 `_04` 的容器就是它的最小前身。
  3. **webui 仍走基线工具面**：可视化页面没有接缝切换开关（它属基线，本阶段未动）；
     若要把"文件世界"搬进页面，接线点已备好（tool_registry 参数）。
  4. **只做了 fs 一条接缝**：M5 的"至少两条"里，Subprocess 接缝是 `_06` 的事——
     同一模式的第二次实践（届时可对照两条例子的相同与不同）。
