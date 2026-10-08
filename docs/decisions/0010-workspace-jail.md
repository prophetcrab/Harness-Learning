# 0010 工作区围栏：策略是独立一层 provider，装饰机制而非侵入机制

- 日期：2026-10-08
- 阶段：P2_Coding/_05_Workspace_Jail（学习计划 M5 第二步）
- 状态：已采纳

## 背景

`_04` 立了 FileSystem 接缝，但两个实现（LocalFS / MemoryFS）都是**机制型**的——
它们只回答"文件怎么存"，不回答"允许在哪里动"。于是出现一个真实的缺口：

- 工具（模型可以随便调）能写 `../escape.txt`：文件跑出工作区；
- 基线的老沙箱（P1 `harness/tools/workspace.py` 的 `_resolve_in_workspace`）
  是在**工具函数里**做的路径检查——如果把那种检查照搬进 `_04` 的 `toolbox.py`，
  策略就又和消费者缠在一起了，换 provider 也换不掉它。

M5 的要求是"至少两个新 provider"，更关键的是要练**策略型 provider**：把"允许
做什么"做成可替换的一层，而不是写死在工具里。dsh 的对应物是 `fs-sandbox`
（`FS_SANDBOX_DENIED`；"confine model file mutations, preserving the local
filesystem's read behavior"）。

## 决策

**1) 策略做成装饰器**（`providers/jail.py`）：`WorkspaceJailFS(base)` 包住任意
FileSystem——写入/编辑先过围栏、其余原样透传。于是**策略与机制正交**：
`WorkspaceJailFS(LocalFS(ws))` 是真实磁盘上的围栏，`WorkspaceJailFS(MemoryFS())`
是内存上的围栏（测试对两种底座跑同一套断言）。这不是"再写一个 provider"，
而是"给 provider 加一层可组合的策略"——与 dsh 把 `fs` / `fs-sandbox` 分成
两个包、策略插件另行 mount 是同一种切法。

**2) 只拦改动、不拦读**：`write_text` / `edit_text` 设卡；`read_text` /
`list_files` 透传（区外文件读得到）。理由：读不是破坏性动作，"限制读"是另一种
产品决策（read-only 模式做的是相反的事：拦写、甚至拦特定读），不该混进围栏的
定义。dsh 的 fs-sandbox 明确写"confine mutations, preserve reads"——边界照抄，
行为就不容易走样。

**3) 判据两层：词法规范化 + 物理复核**：

- **词法层**（对所有底座都生效）：把路径先归一（`\` → `/`），逐段解析 `..`
  （弹栈弹空即逃逸），拒绝绝对路径与盘符路径；`notes/../a.txt` 这类"绕圈但
  仍在区内"的路径规范化后**放行**——规范化结果也传给底层，保证各 provider
  收到同一种路径形态。
- **物理层**（仅底层有真实根时，如 LocalFS）：把规范化路径拼回真实根后
  `resolve()`，仍须落在根内——挡住"工作区里的符号链接指向区外"的绕行
  （`test_symlink_escape_is_denied` 钉死这条；平台上建不了链接则跳过）。

**4) 错误沿用接缝的词汇，而不是新增一套**：拒绝抛 `FsError(FS_SANDBOX_DENIED,
"路径越出工作区，拒绝改动：…")`——错误码命名对齐 dsh；工具层的翻译逻辑
（`_04` 的 `_as_tool_error`）**一行不改**，越权错误与"文件不存在"因此天然同构：
同样的 `{"error", "code"}` 两键、同样的"按 code 分支"用法。这是"新 provider
不加新协议"的具体收益。

**5) 围栏在"执行前一刻"判定，且不产生半程副作用**：越界的 `write_text` 连工作区
目录都不会建（`test_local_denied_write_creates_no_workspace_dir`）；越界的
`edit_text` 直接拒绝、不会先读后拒。判断先于动作，失败无痕。

## 备选与排除理由

1. **把路径检查写回工具函数（像 P1 老沙箱那样）**。排除：那就退回了"策略与消费者
   缠绕"——换 provider 换不掉围栏，也无法验证"三种实现可切换"。接缝的意义正是
   让策略成为一个**可替换的 provider**，而不是工具里的一个 if。
2. **给 LocalFS / MemoryFS 各加一个 `jail=True` 开关**。排除：开关散进机制型实现，
   每个新 provider 都要重写一遍围栏逻辑；装饰器写一次、叠任意底座，且 base 可以
   是第三方 provider 而不必改它的代码。
3. **围栏也拦读（read-only 全锁）**。排除：本阶段的题目是"越出工作目录的**写**被拒"；
   读与写是两种策略，混在一起会让"围栏"的定义变糊。read-only 之类是另一个策略
   provider 的事（dsh 的 sandbox mode 是三个档位，本阶段只做 workspace-write 的
   核心档）。
4. **物理校验用 `os.path.realpath` 全量替换词法层**。排除：realpath 依赖路径实际
   存在（新建文件时父链可能不存在），且内存 provider 没有真实根；词法层保证
   "任何底座上都有基本围栏"，物理层只作为真实磁盘上的补充。两层各司其职。
5. **越界时返回 `None` / 静默改写到区内**。排除：静默改写是伪造模型意图（更危险）；
   返回 None 违反协议。拒绝必须是**响亮且结构化**的错误——模型要能据它自救
   （wiring 测试里模型收到拒绝后换了区内路径重试成功）。

## 后果

- 约束：`WorkspaceJailFS` 的 `workspace` 属性依赖底层是否暴露 `root`（LocalFS 有、
  MemoryFS 无）；物理复核只在有根时启用。`read_text`/`list_files` 的透传意味着
  **区外内容仍对模型可见**（若产品要连读一起锁，需要另加策略——记为已知边界）。
- 收益：策略与机制正交（同一套围栏断言跑两种底座）；三种 provider 切换成为
  一行 `resolve` 的事，且**区内操作在三边结果逐字段相同**（wiring 测试）；越权
  错误与其它文件错误同构；"审批放行 ≠ 放行"的纵深防御有了可执行证据
  （`test_jail_blocks_escape_even_when_approved`：AutoApprove 下越界仍被拒）。
  75 个用例（22 基线 + 53 新增），全项目 546 用例全绿。
- 债务与后续：
  1. **只做 workspace-write 一档**：dsh 的沙箱还有 read-only / danger-full-access
     两档与"单次升级（escalation）"机制；本阶段只做核心档，其余留作选修。
  2. **Windows 路径方言**：词法层按 posix 语义解析再拒绝盘符形态；对
     `\\?\`、UNC（`\\server\share`）等更冷门的形态未逐一测试（都因"含反斜杠/
     盘符"落入拒绝分支，方向安全）。
  3. **真实对话里模型常先自我审查**：提示词已写明工作区约定，实测模型多数时候
     主动换路径——围栏的价值在"它不这么做的时候"；这与 dsh 把沙箱描述为
     可信代码里的策略检查（而非内核边界）的定位一致：防误不防恶，防恶要靠
     更底层（`_06` 的 subprocess 与将来 M8 的审批/沙箱专题）。
