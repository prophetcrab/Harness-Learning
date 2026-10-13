# P3_Coding

P3 阶段的编码工作区。**当前进度：基础包 `mini_harness/` 已落地**——一个
**完全插件化**的 mini harness：内核（服务 / 事件 / effect / inject）+ 能力插件
+ 构件层三层分离，P2 的全部能力以插件形式重新表达。

> P3 的路线定位见根 [README.md](../README.md) 的"未来路线图"。按计划，P3 的
> 能力范围应由"垂类"裁剪；本次先交付**地基**（基础包），后续能力（流式、预算
> 计量、压缩、任务管理……）在这个底座上以插件形式增量补。决策记录见
> [docs/decisions/0020](../docs/decisions/0020-p3-plugin-core.md)。

---

## 1. 这一包解决什么问题

P2 走到了"服务化"，但装配仍是配置树 + 工厂注册表：**能力之间怎么依赖、
装错了怎么办、运行时能不能换**，都由装配代码隐含决定。

P3 基础包把这件事反过来：**内核只认"插件"和"它声明依赖的服务"**。

- 每样能力都是一个插件（`setup(ctx)` + `name` + `inject`）；
- 装配 = 列插件；**装载顺序无关**——依赖齐备的那一刻插件自动激活；
- 卸载 = 回卷：插件装出来的服务、事件订阅、外部注册全部自动撤销；
- 依赖消失（提供者被卸）→ 依赖者自动停用；提供者回来 → 自动重新激活。

一句话：**"装哪些能力"从装配代码的隐含条件，变成内核可检查、可回卷的事实。**

## 2. 三层结构（依赖单向）

```
plugins/     能力插件：把构件装成服务（llm / fs / proc / tools / sessions / prompt / agent）
    ↓ 只 import 下两层
components/  纯构件：从 P1/P2 沉淀的可复用实现（不 import kernel，不认识"插件"）
    ↓ 无依赖
kernel/      插件内核：ctx（服务 / 事件 / effect）+ 插件生命周期（inject 驱动）
```

| 层 | 文件 | 内容 |
|---|---|---|
| `kernel/` | `context.py` | **Context**：服务槽位（provide/retract/require/get）、事件（on/emit/waterfall）、effect 台账（装载即登记、卸载自动回卷、子先父后）、插件生命周期 |
| | `plugin.py` | **Plugin** 协议与声明（name / inject / setup 的读取） |
| `components/` | `llm/ session/ prompt/ tools/ fs/ proc/ agent/` | P1/P2 的构件原样沉淀（provider 接缝、事件日志、提示词装配、工具管线、能力实现、主循环）；`session/projection.py` 合并了 P2 的 `system/message` 遮蔽 |
| `plugins/` | `llm.py fs.py proc.py tools.py sessions.py prompt.py agent.py` | 七个能力插件 + 服务名/事件名常量 + `boot.py` 标准装配 |

`components/` 是本包唯一"搬过来"的部分（来自 P2 冻结基线；只改 import 与两处小升级：
`ToolRegistry.unregister`、事件表加 `system/message`）。

## 3. 插件内核的五件语义

**服务（单槽）**：一个服务名只有一个提供者；重复提供直接报错。
`ctx.get` 是可选依赖（没有就 None），`ctx.require` 是必需依赖（没有就报错）。

**注册即 effect**：装载期（setup 执行中）做的一切——`provide`、`on`、嵌套
`effect`——自动登记撤销动作。插件不必写"我装过什么"，卸载时逐层回卷
（子先父后、同级后进先出）；**装载失败不留半装状态**；某个 disposer 抛错
不打断其余回卷。

**inject 驱动生命周期**：`inject = ("fs",)` 声明依赖。装载顺序无关；依赖
缺失保持 pending（`ctx.pending_plugins` 可查）；提供者被卸载时依赖者**先**退位
（它的清理代码运行期间服务仍可用），提供者回来时自动重新激活。

**事件**：`ctx.on` 订阅（返回退订函数，装载期订阅随 effect 回卷）；
`ctx.emit` 广播（只读通知）；`ctx.waterfall` 瀑布（handler 返回非 None 即改写值）
——后者是插件协作的载体：`prompt/variables` 让插件注入运行时变量，
`llm/request` 让插件改写模型请求。

**boot**：`boot(plugins=[...])` 装载并检查"没有残留 pending"（依赖写错
fail loud）；`Booted.close()` 逆序卸载全部插件。

## 4. 七个标准插件

| 插件 | 提供 | inject | 说明 |
|---|---|---|---|
| `llm` | `llm` | — | FakeLLM / DeepSeek；调用前过 `llm/request` 瀑布，调用后广播 `llm/response` |
| `fs` | `fs` | — | LocalFS / MemoryFS / WorkspaceJailFS（插件名带实现后缀） |
| `proc` | `subprocess` | — | LocalSubprocess / ScriptedSubprocess（**可选**能力） |
| `tools` | `tools` | `fs` | 组装工具面（8 件：calculate + read/write/list/edit + search/find + 可选 shell）；卸载时注销注册表 |
| `sessions` | `sessions` | — | 打开即恢复 + 崩溃尾行修复 + 列举 + 分叉 |
| `prompt` | `prompt` | — | section 装配 + `prompt/variables` 瀑布 + 装配单 |
| `agent` | `agent` | `llm` `tools` `sessions` `prompt` | 会话管理：`ask(text, session_id)` 跑一个 turn |

## 5. 运行方法

```bash
cd P3_Coding/mini_harness
python -m pytest -q                       # 40 个用例（全离线）

# 离线组合（默认）：FakeLLM + MemoryFS + 剧本命令
python chat.py
python chat.py --ask "帮我算 2+3"
python demo.py                            # 六节演示：装配 / 台账 / 对话 / resume / 热插拔 / 回卷

# 真实组合：DeepSeek + 真实磁盘 + 真实进程（读项目根 .env 的 key）
python chat.py --realtime
python chat.py --realtime --fs jail --ask "用 shell 看看当前目录"
python chat.py --realtime --no-approve    # 需审批操作自动放行（谨慎）

# 一键启动器
./run.bat            # 离线 demo；./run.bat chat 对话；./run.bat test 跑测试
bash run.sh chat --realtime
```

会话内命令：`/plugins`（插件台账与状态）、`/services`、`/tools`、`/history`、`/exit`。

**门禁**（在 `P3_Coding/` 下）：`python scripts/check.py`（ruff + pytest；mypy 可选）。

## 6. demo 里看得到的东西

`python demo.py` 六节，全离线：

```
0) 装载：故意打乱插件顺序 → 全部就绪（inject 驱动，顺序无关）
1) 台账：插件 / 服务 / 8 件工具面
2) 对话：一个 turn 的完整事件流（含工具调用）落盘 session.jsonl
3) resume：重新装配后同名会话续聊，turn 编号接续
4) 热插拔：卸载 fs → tools/agent 自动退位（pending）；换入 LocalFS → 自动重激活
5) 卸载：整体回卷——服务、订阅、工具面全部归零，无残留
```

## 7. 与 P2 的关系

- **能力等价**：P2 能做的（提示词装配与追溯、三条接缝、会话日志、工具面、
  审批 fail-closed、离线/真实双组合）本包都有，工具面同为 8 件；
- **装配变了**：P2 用 YAML 配置树 + 工厂注册表；P3 用**插件列表 + inject**。
  P2 的配置分层（`profiles` / `--dump-config`）尚未移植——它是"配置数据 → 插件"
  的一层，在 P3 底座上重做属于后续工作；
- **服务化尚未移植**：RPC / 事件流跟随 / 可视化页面属于平台档，未包含在基础包内。

## 8. 已知边界（本包**没有**的东西）

- 流式输出（`complete()` 仍是一次性返回）；无重试与错误分类；
- 没有预算计量、上下文压缩、spill、任务管理（goal/plan/todo）；
- 事件无优先级/异步；中间件同步串行；单进程单写者（无写锁）；
- 插件依赖是"服务名存在性"检查，没有版本约束、没有环形依赖检测
  （环形会成组退位，不会死循环，但语义未定义——见决策记录"后果"）。
