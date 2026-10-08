# 0011 命令接缝：结果与异常分离，超时杀整棵树

- 日期：2026-10-08
- 阶段：P2_Coding/_06_Subprocess_Seam（学习计划 M5 第三步，**M5 收官**）
- 状态：已采纳

## 背景

fs 接缝（`_04`/`_05`）立起之后，M5 还剩"命令执行"这条能力。P0/P1 时代有 `shell`
工具，但它是工具函数里直接 `subprocess.run()` —— 既不可替换，也没处理超时语义、
输出上限、凭证泄露这些真实问题。本阶段把命令执行也做成三角色接缝，并且借第二次
实践检验 `_04` 立的那套模式（Definition / Provider / Consumer / 显式解析）是否
真的通用。

dsh 的参考给出两条关键设计（`ctx.subprocess` / `ctx.shell`）：**callers own
deadlines**（期限由调用方解析，不是 provider 的隐式默认）；**only infrastructure
failures reject**（只有基础设施失败才是异常，命令自己的失败是结果）。本阶段照此
落地。

## 决策

**1) 结果与异常分离——`CommandResult` 是主词汇**（`providers/subprocess.py`）：
一次执行的结局是 `CommandResult(exit_code, stdout, stderr, timed_out)`。**非零退出、
超时被杀都是"结果"**：它们要进日志（铁律 #1）、要进模型历史让模型读报错改命令
（铁律 #7）；只有"起不来"（cwd 不存在、shell 不可用）才抛
`SubprocessError(SHELL_SPAWN_FAILED)`。这条边界让"命令失败"成为可控的信息流，
而不是打断循环的异常。

**2) 超时杀整棵进程树**（`subprocess_local.py`）：`shell=True` 下直接子进程是
shell、命令本体是它的子进程。只杀 shell 的后果是真实的——本项目实测：设 0.6 秒的
超时，命令本体（`python sleep 30`）继续活着并占着输出管道，捕获要等 30 秒才返回，
甚至被工具管线的 30 秒超时抢先（错误码串成 `TOOL_TIMEOUT`）。修法：Windows 用
`taskkill /F /T /PID`，POSIX 用 `start_new_session` + `killpg`——terminate the
full managed process range（dsh 的原话）。代价诚实记录：硬杀会丢未刷出的缓冲
输出（"杀"的固有语义），已刷出部分照常带回。

**3) 消费侧约束在组装时解析**（`toolbox.py` 的 `build_toolbox`）：模型只给
`command` 一个参数；**工作目录** = 会话工作区、**期限**（默认 15 秒）、
**输出上限**（默认 2 万字符，超出截断并附说明）全部在组装时定死，作为显式参数
传给 provider（callers own deadlines）。理由与 `_05` 的围栏一致：模型能改的东西越少，
"跑之前就知道会发生什么"越可靠；输出上限则防止单条事件把会话日志炸掉。

**4) 凭证不进子进程**（`LocalSubprocess._child_env`）：子进程默认继承本进程环境，
`DEEPSEEK_API_KEY` 会原样泄给任意命令。默认黑名单剔除这些键（dsh："child
environments remove ambient credentials"）；测试用真实子进程验证"读不到 key，
但其余变量保留"。黑名单而非白名单：Windows 上 `SystemRoot` 等变量缺失会让 cmd
直接起不来。

**5) 消费者沿用同一套错误翻译**：`shell` 工具把三种结局翻成与文件错误同构的
`{"error", "code"}`（`SHELL_NONZERO_EXIT` / `SHELL_TIMEOUT` / `SHELL_SPAWN_FAILED`）。
"新增一条接缝不必新增一套错误协议"——这是 `_04` 定的形状第二次被直接复用。

**6) 两个安全默认**：`shell` 工具标 `needs_approval`（执行任意命令 ≥ 写文件，
先过审批；拒绝则命令根本不执行——wiring 测试用"写标记文件"验证）；`ScriptedSubprocess`
让离线路径**一条命令都不真跑**（chat.py 的 `--fake` 自动切换到它），测试因此
不依赖平台命令的实际行为。

## 备选与排除理由

1. **超时抛异常、让调用方 try/except**。排除：超时是命令生命周期里完全正常的
   结局，做成异常会把"处理超时"和"处理崩溃"混在一起；且结果词汇里 `timed_out`
   能让模型明确回答"是不是没跑完"，比异常栈有用。
2. **只杀直接子进程（简单 kill）**。排除：实测会挂 30 秒（见上），是真实 bug 不是
   理论洁癖；且 dsh 明确把"terminate the full managed process range"写进契约。
3. **白名单环境变量**（只放 PATH 等少数几个）。排除：Windows 上 cmd/子进程依赖的
   变量比想象多，白名单会大面积把命令搞坏；黑名单+默认剔凭证是风险与可用性的
   平衡点，且 `blocked_env` 可配。
4. **把 cwd / 期限做成工具的 schema 参数**（让模型自己填）。排除：那就把
   "在哪里跑、最多跑多久"交给了模型——与 `_05` 的教训相反。dsh 的 resolve 步骤
   精神是"跑之前把约束变成显式的"（显式 ≠ 模型可改）。
5. **输出不设上限**。排除：一条 `yes` 就能产生 GB 级输出；截断+长度说明既保序
   又保住日志，与 dsh 的 bounded output 同向。
6. **不剥凭证，靠审批防泄露**。排除：审批防的是"要不要跑"，不是"跑起来后能看到
   什么"；纵深防御里这两个是独立维度。

## 后果

- 约束：`LocalSubprocess` 只做机制不做策略——命令白名单/目录限制将来若是需要，
  走 `_05` 式的策略 provider，不往这里加。Windows 上超时经 taskkill 子进程实现，
  会多起一个短命进程（可忽略）；POSIX 走进程组，行为更干净。
- 收益：**同一套工具测试在真实现与剧本实现下全绿**（`isinstance` 契约 + 请求记录
  断言 cwd/期限由组装决定）；三种失败结局全部以模型可读的结构化结果进入日志与
  历史；**"审批放行 ≠ 放行"在命令侧复验**（拒绝则零执行）。66 个用例（22 基线 +
  44 新增），全项目 568 用例全绿；跨 `_01`–`_06` 同进程 364 用例无串味。
- 债务与后续：
  1. **后台进程 / 流式输出未做**：dsh 的 `ctx.shell` 有 background + 轮询；本阶段
     只做前台有界执行（学习计划里 M5 的验收只要求捕获+超时）。
  2. **超时是硬杀**：没有"先 SIGTERM 宽限再 SIGKILL"的两段式（dsh 的 termination
     grace）；Windows 上 taskkill /F 本身即硬杀。命令收到 SIGTERM 做清理的场景
     会在后续需要时补。
  3. **命令策略未做**：本阶段 shell 什么命令都能跑（除审批外无限制）；这与 `_05`
     的围栏理念一致——策略是独立一层，将来可组合一个"命令白名单" provider
     （或复用 sandbox-policy 思路）。
  4. **webui 仍走基线工具面**（无 shell 开关）；接线点（`tool_registry` 参数）
     已备好。
