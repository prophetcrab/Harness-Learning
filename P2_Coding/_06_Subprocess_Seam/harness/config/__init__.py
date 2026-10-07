"""harness.config —— M6：profile 式组装（占位，待实现）。

目标：能力组合从代码变成配置数据；一行 patch 完成 provider 替换。

规划：
- 极简插件协议：`Plugin.setup(ctx) -> disposer`；注册即 effect（卸载自动回卷）；
- `profiles/*.yaml`：有序层 + 按 id patch（替换整块 config 或 insert 新行）；
  顺序：base → profile patch → 用户 patch → CLI --patch；
- `python -m harness --profile <name> dump-config`；
- 两个 profile：dev（FakeLLM + MemoryFS）、prod（DeepSeek + 本地）。

验收：同 base 两个 profile 产出不同且可读的树；换 LLM provider 只改一行；
故意写错配置启动即报错并指出位置。决策记录：0008-profile-composition。
"""
