# 0012 插件与 effect：注册自动登记撤销，故障不留半装状态

- 日期：2026-10-09
- 阶段：P2_Coding/_07_Plugin_Effect（学习计划 M6 第一步）
- 状态：已采纳

## 背景

M5 结束时，能力的装配是 `_04` 的 `ServiceContainer` 手工登记或 `_06` 的
`build_toolbox(...)` 直接构造——**装完就完了，没有"拆"的概念**：想换掉 fs provider
只能重新构造整个工具面；装配到一半失败会留下什么、进程退出要不要清理，都没有答案。

M6 的目标是"能力组合从代码变成配置数据"（`_08`/`_09` 的事），而配置化组装的前提是
**装配动作可回卷**：配置要能整体装载、整体卸载、按层覆盖、失败回退。dsh 的底座
Cordis 把这一点立为核心思想之一——"Registrations are reversible effects：Prompt
sections、tool schemas、adapters、providers、listeners 都通过 `ctx.effect()` 安装，
reload 和 teardown 时按预期解开"。本阶段就是给 mini-harness 立下这条。

## 决策

**1) `Context` 是"注册中心 + 装配台账"两合一**（`kernel/context.py`）：命名槽位
（`provide`/`retract`/`require`/`get`）承载"供体登记了什么"；effect 台账
（`effect`/`register`/`unload`）承载"谁装的、怎么拆"。与 `_04` 的 ServiceContainer
相比：单槽语义保留（重复注册 fail loud），多了撤销通道。

**2) 注册即 effect——`provide` 在装载期自动登记撤销**：这是本阶段最关键的一条。
插件作者写：

    def setup(ctx):
        ctx.provide("fs", LocalFS("ws"))

就足够了——不需要额外写 `return lambda: ctx.retract("fs")`。装载期（install 执行中）
的 `provide` 会把"撤销槽位"自动挂到当前 effect 的 disposer 列表上。**为什么**：
让"注册"和"撤销"在同一个动作里发生，插件作者不可能忘掉配对的撤销动作；这也是
铁律 #3 的字面落实。副作用之一是装载中途崩溃也能干净回卷（provide 已经登好账）——
demo 0b 与 `test_failed_install_leaves_nothing_behind` 都验证了这一点。
（早先版本要求 setup 显式返回 disposer；实测发现"插件崩在 provide 之后"会留
半装槽位，这推动了本决策——**故障语义是设计的试金石**。）

**3) effect 成树，卸载子先父后**：装载期注册的 effect 自动挂到当前 effect 之下。
插件内部再注册小 effect（如"注册一个工具"、"订阅一个事件"）是常见形态；没有层级
就得靠人工记住拆的顺序。有了树：卸载父 → 子先逐层回卷（同级后进先出）→ 再回卷
父自己的 disposer。对应 Cordis："If teardown order matters, keep the related
work in one effect so disposal unwinds in the intended sequence."
（同样来自实测：第一版没有父子关系，"卸载外层"留了内层没拆——冒烟测试抓出来的。）

**4) 回卷永不半途而废**：单个 disposer 抛异常 → 记下来，**继续回卷其余**，最后把
第一个异常抛出去；装载失败 → 回卷已登记的（含子树）再抛；批量装载
（`load_plugins`）→ 任一失败则逆序卸掉已装成功的部分。理由：半装状态比一个异常
更难排查；"尽量拆干净、然后把问题报出来"是容器/事务系统的通行纪律。

**5) 装载器极简、协议鸭式**（`kernel/plugin.py`）：插件的全部要求是"有 setup(ctx)
方法"或"自身可调用"——不要求继承任何基类（与项目中 LLMProvider/FileSystem 的
Protocol 风格一致）。`load_plugin` 把 setup 包成一个 effect；插件名取 `name`
属性（或函数名），effect 名形如 `plugin:toolbox`，卸载时用它。

**6) 不改 `harness/`，新旧装配并存**：本阶段只新增顶层 `kernel/` 与
`providers/plugins.py`；`_04`–`_06` 的 `ServiceContainer`/`build_toolbox` 直通路径
原样保留（不想引入 kernel 的调用方/旧入口继续可用）。推荐路径（chat.py）切到插件
装载，退出时演示整体回卷。

## 备选与排除理由

1. **让插件手写 `return disposer`（不自动登记）**。排除：实测会留半装状态
   （provide 之后崩）；而且写撤销动作是重复劳动，容易漏。"注册即 effect"的核心
   价值就是**不需要记得拆**——自动登记把正确性做进机制里。
2. **扁平 effect 列表（不做树）**。排除：装载期注册子 effect 时无法归因——卸载
   外层时不知道内层属于谁；实测第一版就漏拆了内层。树是"整体拆卸"的最小结构，
   Cordis 的 effect 也是 per-context 的层级结构。
3. **回卷遇错即停**。排除：第一个 disposer 出错后剩下的注册物永远留着（资源泄漏），
   而错误本身已经要抛给调用方了——继续拆完只是更接近"干净"。
4. **提供 inject 依赖声明 / 事件系统 / Service 生命周期（照抄 Cordis 全套）**。
   排除：本阶段只练"注册即 effect"一个机制（P2 的每个阶段只加一个）；inject 之类
   留给需要时（学习计划里 M6 的范围就是组合与配置）。依赖缺失目前用
   `ctx.require` fail loud 就够——装载顺序错误在装载时暴露。
5. **卸载时"收回已发出的对象"（让会话手里的工具面失效）**。排除：那需要引用计数/
   代理层，复杂度远超教学目标；明确语义是"卸载管理装配台账"，已发出的对象按其
   生命周期独立存活（`test_session_keeps_working_after_ctx_unload` 钉死这条语义）。

## 后果

- 约束：`kernel/` 是纯逻辑包（不依赖 harness/prompt/context/providers），可被任何
  地方引用；`providers/plugins.py` 依赖 kernel 与 providers 自身，方向单一。
  `provide` 在装载期自动登记的行为需要调用方知道（文档与 docstring 都写明：
  effect 外的 provide 是手工管理）。
- 收益：**装配第一次有了"拆"**——整体/单个卸载、故障回卷、嵌套顺序全部有测试与
  demo 覆盖（57 用例：22 基线 + 35 新增）；`/ctx`、`/unload`、退出回卷让"注册即
  effect"在真实对话里可见；`_08` 的 profile 分层有了可以组装/回退的装配底座。
  全项目 603 用例全绿；跨 `_01`–`_07` 同进程 421 用例无串味。
- 债务与后续：
  1. **依赖注入未做**：`require` 是即时检查（装载顺序由调用方保证）；Cordis 的
     inject 是"等服务就绪再激活"。当前用 `load_plugins` 的列表顺序表达依赖，
     够用但不优雅——留给"要写更复杂插件"时（大概率是 P4 的自研内核阶段）。
  2. **webui 未接插件装配**（沿袭前几阶段的边界）；接线点是 tool_registry 参数。
  3. **stderr 解码边界**：真实 API 冒烟里发现 Windows `dir` 的中文输出按 UTF-8
     解码会乱码（cmd 用 GBK）——`LocalSubprocess` 目前固定 utf-8+replace。
     跨平台编码策略留作后续（dsh 的做法是输出按字节收集、渲染层再定编码）。
