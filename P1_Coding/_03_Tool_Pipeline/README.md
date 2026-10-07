# _03_Tool_Pipeline —— 工具系统：注册表 + 审批 + pre/execute/post 管线

P1 第三个练习，对应学习计划 **M2**：把工具从 _01 的"注册 + 查表 + 错误包装"
（`Toolbox`）升级成 dsh 式的**注册表 + 执行管线**。

## 学习目标

1. 看清 **定义与实现分离**：`define_tool()` 只声明"是什么"（名字/说明/参数 schema /
   是否需审批），实现函数单独注册。同一份定义可绑不同实现（真实 / 假实现 / 沙箱实现）。
2. 掌握 **pre → execute → post 三段式管线**：决策（allow/deny/ask + 改写参数）→
   执行（含超时）→ 加工（截断、脱敏、附加上下文），每段都是可插拔的中间件链。
3. 理解 **审批是策略接口**：管线只依赖 `ApprovalPolicy.approve()`；三态（allow / deny /
   ask）明确区分；**问不到应答方一律 fail-closed 拒绝**。
4. 体会 **注册表分层 + 遮蔽**：作用域层同名遮蔽全局（most-specific-wins），
   作用域撤下后全局定义自动重新可见（铁律 #4）。
5. 验证 **接缝的价值**：`ToolPipeline.execute()` 恰好满足 `AgentLoop` 的执行器契约，
   管线原封不动插进主循环，`agent_loop` 一行都不用改。

## 文件（推荐阅读顺序）

| 顺序 | 文件 | 内容 | 对应 dsh |
|---|---|---|---|
| 1 | `tool_pipeline/registry.py` | 注册表：定义/实现分离、两层作用域遮蔽 | `core/tools/src/index.ts` + `core/scope` |
| 2 | `tool_pipeline/schema.py` | pydantic 模型 → JSON Schema | `docs/cookbook/adding-a-tool.md` |
| 3 | `tool_pipeline/approval.py` | 审批策略协议 + Auto/Deny/Scripted/Prompt | `interaction/user-approval` |
| 4 | `tool_pipeline/pipeline.py` | ★ 核心：pre/execute/post + 超时 + ToolResult | `docs/tool-execution-pipeline.md` |
| 5 | `tool_pipeline/middlewares.py` | 示例中间件：DenyTools / RequireApproval / TruncateOutput | `guard/`（repeat-tool-reminder 等） |
| 6 | `tool_pipeline/builtin.py` | 内置工具 + `build_default_registry()` | `tools/` 内置工具 |
| 7 | `tool_pipeline/__init__.py` | 包出口 | — |
| — | `agent_loop/`、`llm_seam/` | 自包含副本（来自 _02/_01，未修改） | — |
| — | `demo.py` | 入口：`--fake` / `--pipeline-only` / 默认真实 API | — |
| — | `test_tool_pipeline.py` | 19 个验收测试（全离线） | — |

> `agent_loop/` 与 `llm_seam/` 是 _02/_01 同名包的原样副本，使本练习完整独立。
> `agent_loop` **未做任何改动**——管线通过执行器契约接入，这正是接缝价值的体现。

## 运行

```bash
cd P1_Coding/_03_Tool_Pipeline

# 只演示管线本身（不需要 key，最快理解）
./run.bat --pipeline-only
bash run.sh --pipeline-only

# 离线剧本（不需要 key）
./run.bat --fake

# 真实 API（需要项目根 .env 里的 key）
./run.bat "帮我算 1234*56.78"

# 测试（全离线）
python -m pytest -q        # 19 个用例
```

## 验收标准（对应 M2 三件套）

- [x] **pre 拒绝短路执行**：`DenyTools` 命中后工具函数根本不被调用（测试数计数为 0）
- [x] **超时返回结构化错误**：`execute` 段超时返回 `status=timeout` + `TOOL_TIMEOUT`
- [x] **工具异常转结构化错误回给模型**：异常被包装成 `{"error": ...}`，不中断循环
- [x] **审批三态**：allow 放行 / deny 短路 / ask 走策略；批准与拒绝路径都验证
- [x] **fail-closed**：无审批策略、非交互终端，一律拒绝（绝不默认放行）
- [x] **post 加工**：中间件可替换结果（`TruncateOutput` 截断超长输出）
- [x] **参数改写**：pre 中间件改参数后，后续中间件与执行器都看到新值
- [x] **注册表分层**：作用域遮蔽全局、撤下后重新可见、不污染全局
- [x] **定义/实现分离**：同一份定义可绑不同实现；全局重名 fail loud
- [x] **与 AgentLoop 集成**：审批拒绝后模型不重试、如实收尾；批准路径真正执行
- [x] **工作区沙箱**：`write_file` 路径越界直接拒绝（审批放行也不许）
- [x] `pytest -q` 全部通过（19 个用例，0.19 秒）

## 管线一览（`--pipeline-only` 输出）

```
[1] 正常成功：calculate            → {'result': 70066.52}
[2] 执行超时：sleep 2s / 预算 0.3s  → {'error': ..., 'code': 'TOOL_TIMEOUT'}
[3] post 截断：echo_long 超长输出    → {'text': '...已截断...', 'truncated': True}
[4] 审批三态：write_file
    批准 → {'written': 'note.txt'}    拒绝 → {'error': '用户拒绝', 'denied': True}
    无策略 → {'error': '...fail-closed 拒绝', 'denied': True}
[5] 沙箱越界：'../escape.txt'       → {'error': '路径越出工作区，拒绝写入'}
```

## 三个值得记住的设计点

1. **三段各有明确职责，且都是"链"**：pre 可以多个中间件接力（分类 → 校验 → 审批），
   参数改写会沿链传递；execute 只管跑和超时；post 只管加工结果。关注点分离，各自可测。
2. **审批缺省是拒绝**：这是安全默认值。`ToolPipeline(approval=None)` 或非交互终端下，
   需要审批的工具一律 `denied`——宁可任务没做完，也不默认放行危险操作。
3. **沙箱与审批是两道独立的防线**：审批是"人同意"，沙箱是"无论同不同意都不许"。
   `write_file` 两道都有：即使 `AutoApprove` 放行，`../` 越界仍被 `is_relative_to` 挡住。

## 已知限制（故意的，后续主题解决）

- 中间件是同步的、串行的；没有 dsh 的 waterfall 事件优先级/取消传播；
- 超时靠线程 join，超时后工具线程仍在后台跑（无法强杀）；真正的子进程超时留到 M5 的
  `SubprocessService`；
- 注册表只有两层（全局 + 单作用域），没有多级继承；
- 历史仍在内存（`_04_Session_Log` 做持久化 + resume）。
