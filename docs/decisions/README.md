# 决策记录

每个非平凡练习完成时，在这里留一份决策记录。模仿 dsh 的 Agent Notes：
**记录"为什么这样选"，而不是"做了什么"**——做了什么看代码和练习 README。

## 命名

`NNNN-kebab-标题.md`，编号全项目递增、不复用。例如 `0001-turn-and-step.md`。

## 模板

```markdown
# NNNN 标题

- 日期：YYYY-MM-DD
- 练习：P1_Coding/_0X_...
- 状态：已采纳 / 已被后续记录取代

## 背景

（面临什么问题；不做这个决定会怎样）

## 决策

（选了什么，一两句话讲清）

## 备选与排除理由

（至少一个认真考虑过的替代方案，以及为什么没选）

## 后果

（这个决定带来什么约束、债务、后续动作）
```

## 规则

- 一个决定一份记录；后续变更写新记录，旧记录不改（可与 dsh 一样标注"已被 NNNN 取代"）。
- 记录里引用代码时用相对路径，便于跳转。
- 同一天有多份记录也正常，编号保持递增即可。

## 已有记录

- [0001 LLM 接缝：协议先行、provider 翻译方言](0001-llm-provider-seam.md)（P1 `_01`）
- [0002 turn 与 step：循环抽成类，轨迹成为一等返回值](0002-turn-and-step.md)（P1 `_02`）
- [0003 工具执行管线：三段式中间件链，审批 fail-closed](0003-tool-pipeline.md)（P1 `_03`）
- [0004 会话日志：日志是唯一真相，消息由投影派生](0004-event-sourcing.md)（P1 `_04`）
- [0005 mini harness 组装：接线而非新增机制，副本而非共享包](0005-mini-harness-assembly.md)（P1 `_05`）
- [0006 提示词装配：有序 section 注册表 + 作用域遮蔽](0006-prompt-sections.md)（P2 `_01`）
- [0007 运行时上下文：每 step 渲染、变化才记录、遮蔽式投影](0007-prompt-context.md)（P2 `_02`）
- [0008 装配单：把"怎么拼出来的"记进日志，重建校验有牙齿](0008-prompt-trace.md)（P2 `_03`）
- [0009 FileSystem 接缝：三角色分离，机制与策略分层](0009-filesystem-seam.md)（P2 `_04`）
- [0010 工作区围栏：策略是独立一层 provider，装饰机制而非侵入机制](0010-workspace-jail.md)（P2 `_05`）
- [0011 命令接缝：结果与异常分离，超时杀整棵树](0011-subprocess-seam.md)（P2 `_06`）
- [0012 插件与 effect：注册自动登记撤销，故障不留半装状态](0012-plugin-effect.md)（P2 `_07`）
- [0013 profile 分层：装配变成数据，patch 整块替换不合并](0013-profile-layers.md)（P2 `_08`）
- [0014 配置可见：字段级来源台账，错误带层与位置](0014-dump-config.md)（P2 `_09`）
- [0015 stdio JSON-RPC：服务化第一刀，握手门禁与 fail-closed](0015-rpc-transport.md)（P2 `_10`）
- [0016 事件流跟随：follow = 重放 + 订阅，断线补齐靠日志](0016-session-follow.md)（P2 `_11`）
- [0017 编码工具三件：能力面补齐在既有接缝上，不引入新机制](0017-coding-tools.md)（P2 `_11`）
- [0018 P2 形态可视化页面：子类化基线 Handler，装配全部走配置](0018-p2-webui.md)（P2 `_11`）
- [0019 P2 整合包：成品快照单独成目录，测试重命名防重名](0019-p2-package.md)（P2 收官）
- [0020 P3 基础包：把整个 harness 重表达为插件（内核 + 构件 + 插件三层）](0020-p3-plugin-core.md)（P3 地基）
- [0021 界面即插件：webui 做成插件（装载即起、卸载即停），离线模型分两档](0021-webui-as-plugin.md)（P3 地基）
- [0022 RPC 即插件：JSON-RPC 服务端做成插件（follow = 重放 + 订阅）](0022-rpc-as-plugin.md)（P3 地基）
- [0023 自扩展工具：让模型装卸自己所在进程的插件（白名单 + 审批）](0023-self-tools.md)（P3 `_01`）
- [0024 手动审批 + 从工作区安装插件：在浏览器里看代码批准](0024-manual-approval-install.md)（P3 `_01`）
- [0025 流式输出：可选能力接缝 + 纯函数聚合 + 瞬态事件](0025-streaming.md)（P3 `_02`）
- [0026 用量与预算：计量是事实（进日志），超限是结果（结构化收尾）](0026-usage-budget.md)（P3 `_03`）
- [0027 上下文仪表：先看见，再动手（容量 / 构成 / 缓存命中）](0027-context-meter.md)（P3 `_04`）
- [0028 上下文压缩：日志只追加"折叠声明"，投影里旧区段变一条摘要](0028-compaction.md)（P3 `_05`）
- [0029 界面拆分、快捷命令菜单与可选依赖：optional 依赖声明 + 页面三件套](0029-webui-menu-and-optional-deps.md)（P3 `_05`）
- 0030 任务清单：整表快照进日志，模型不带序号、人带序号 ——（P3 `_06`，**已整体回退**；
  决策文件随代码一起撤掉，编号留空以保留历史。见 [PLAN.md](../../P3_Coding/PLAN.md) 的「`_06` 的两次回退」）
- [0031 插件声明纪律：把「约定」变成「不变量」（三处耦合修复）](0031-plugin-declaration-discipline.md)（P3 `_05` 收尾）
- [0032 壳的两条扩展点：显示自述与路由自述（界面零知识）](0032-shell-surfaces.md)（P3 `_05` 收尾）
- [0033 插件管理：把"哪些插件是地基"变成可点击的事实](0033-plugin-admin.md)（P3 `_05` 收尾）
- [0034 turn 执行器：并行会话、刷新不掉、可中断](0034-turn-runner-concurrency.md)（P3 `_05` 收尾）
- [0035 角色由装配方定：base / capability 是应用的性质](0035-assembly-declares-role.md)（P3 `_05` 收尾）
- [0036 工具插件化：底座与内容分开（一件事一个插件）](0036-tool-plugins.md)（P3 `_06`）
- [0037 管理弹层由自述驱动：壳内置一页只读，插件声明即可顶替](0037-self-described-admin-panel.md)（P3 `_06`）
- [0038 源码即插件：入口判据、署名与审批预览的三处实测教训](0038-loader-owner-approval-lessons.md)（P3 `_06`）
- [0039 用户报告的三处修复：端口争抢、面板闪烁、被截断的日志](0039-port-panel-and-log-robustness.md)（P3 `_06`）
- [0040 P3 收尾：交付清单、与 dsh 的三处差距、将来接着做的地方](0040-p3-wrap-up-vs-dsh.md)（P3 收官）
- [0041 P4 定调：实质化 —— 自有最小插件底座 + 三类插件 + 一个项目](0041-p4-substantiation.md)（P4 目标）
- [0042 P4 基座边界：支持「everything is plugins」的最小底座](0042-p4-base-boundary.md)（P4 基座，**草案**）
- [0043 P4 全部重写：不搬 P3 代码，逐个小步写](0043-p4-rewrite-from-scratch.md)（P4 方法）
- [0044 LLM 是路由器：模型选择是数据，不是装配](0044-llm-router-and-model-selection.md)（P4 M2 第一步）
- [0045 LLM 能力组：主插件 + 三个子插件（provider / retry / tokens-usages）](0045-llm-capability-group.md)（P4 M2）
- [0046 提示词：base 装配器 + 两个能力插件（角色模板 / 检查与编辑）](0046-prompt-plugins.md)（P4 M2）
- [0047 会话 / 持久化 / 工作区 / 项目](0047-sessions-workspace-project.md)（P4 M2）
- [0048 工具面：tools-admin + 六个工具插件 + 两层文件策略](0048-tools-and-fs-policies.md)（P4 M2）
- [0049 Agent 三件套：agent / agent-loop / agent-admin](0049-agent-trio.md)（P4 M2）
- [0050 最小应用：launcher + 装配单 → 一个 AI 服务](0050-minimal-app.md)（P4 M2 收尾）
- [0051 会话日志对齐 dsh：时间戳 / header / 目录布局 / 读写句柄 / 未知事件](0051-session-log-alignment.md)（P4 M2 收尾）
- [0052 去掉 components 层：共享类型按功能域并入插件模块](0052-remove-components-layer.md)（P4 架构修正）
- [0053 插件间依赖方向：只允许由外向内，兄弟能力不互相 import](0053-plugin-dependency-direction.md)（P4 架构修正）
- [0054 装配单支持 YAML：数据与代码白名单分开](0054-yaml-manifest.md)（P4 M3 起步）
- [0055 批量脚本改源码的教训：不在未提交的工作区上做机械重写](0055-batch-script-lesson.md)（P4 恢复记录）
- [0056 分发形态：pip install p4-harness 后 import harness，启动器随 core 分发](0056-packaging-and-public-surface.md)（P4 M3）
