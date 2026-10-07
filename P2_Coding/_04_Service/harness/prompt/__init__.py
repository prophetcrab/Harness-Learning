"""harness.prompt —— M4：系统提示与上下文装配（占位，待实现）。

目标：提示词是可组合、可追溯、可重建的产物。

规划：
- section 注册表：有序、支持作用域覆盖；
- 变量插值：{{cwd}} / {{platform}} / {{time}}；
- 每 step 渲染运行时上下文，system/message 进日志；
- 装配断言：渲染出的提示词必须能由「日志 + 装配器」重建；
- CLI：python -m harness run --dump-prompt 打印装配过程与来源。

验收：快照稳定、改 cwd 只影响对应 section、新增 section 不动其他部分。
决策记录：0006-prompt-assembly。
"""
