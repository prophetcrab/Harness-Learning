# mini_harness —— P3 基础包（完全插件化的 mini harness）

P3 的**地基**：一个把"能力"全部表达为插件的 mini harness。三层分离、
装载顺序无关、卸载自动回卷、运行时热插拔——P2 的全部能力都在这个底座上以
插件形式重新表达。

> 本目录是 P3 的**开发位置**（与 P2 的 `mini_harness/` 快照不同）。
> 阶段定位与路线见 [../README.md](../README.md)。

## 快速开始

```bash
cd P3_Coding/mini_harness
python -m pytest -q                    # 40 用例，全离线
python demo.py                         # 六节演示（装配 / 台账 / 对话 / resume / 热插拔 / 回卷）
python chat.py --ask "帮我算 2+3"       # 离线一个 turn
python chat.py                         # 离线交互
python chat.py --realtime              # 真实组合（DeepSeek + 磁盘 + 进程；需 .env）
./run.bat                              # 一键：demo；run.bat chat / run.bat test
```

## 目录

```
kernel/        插件内核（纯逻辑，不认识任何业务词汇）
  context.py     Context：服务槽位 + 事件（emit/waterfall）+ effect 台账 + 插件生命周期
  plugin.py      Plugin 协议与声明（name / inject / setup）
components/    纯构件（P1/P2 沉淀；不 import kernel）
  llm/           中立词汇 + provider 协议 + FakeLLM / DeepSeekProvider
  session/       事件日志（append-only + JSONL + 崩溃修复 + 投影）
  prompt/        section 注册表 / 插值 / 装配单 / 运行时上下文 / 每 step 渲染包装
  tools/         注册表 + pre/execute/post 管线 + 审批策略 + 参数模型 + 计算器
  fs/ proc/      FileSystem 与 Subprocess 接缝的实现
  agent/         主循环（turn/step + 轨迹 + 取消）
  env.py         .env 与真实 provider 构造
plugins/       能力插件（把构件装成 ctx 服务）
  services.py    服务名常量；events.py 事件名常量
  llm.py fs.py proc.py tools.py sessions.py prompt.py agent.py
  boot.py        boot / boot_offline / boot_realtime
tests/         40 个用例（内核 21 + 插件 10 + 端到端 9）
chat.py demo.py run.bat run.sh conftest.py
```

## 核心概念（读代码前先看这几条）

**插件**：一个有 `setup(ctx)` 方法的对象（或普通函数），可声明 `name` 与
`inject`（依赖的服务名列表）。

**服务**：`ctx.provide(name, value)` / `ctx.require(name)`；单槽、重复 fail loud。

**注册即 effect**：装载期做的一切（provide / on / 嵌套 effect）自动登记撤销；
卸载时"子先父后、后进先出"回卷；装载失败不留半装状态。

**inject 驱动**：依赖齐备即激活，缺失即 pending；提供者退位时依赖者先停用，
提供者回来时自动重新激活。**装载顺序无关**。

**事件**：`emit` 广播；`waterfall` 瀑布（可改写值）——`prompt/variables`
与 `llm/request` 是仅有的两个瀑布。

**打开即恢复**：`agent.ask(text, session_id)` 对不存在的会话是创建、对已有的
是恢复——同一条路径（装载日志 → 投影历史 → 建循环）；历史永远由日志投影而来。

## 写一个自己的插件

```python
from kernel import Context
from plugins import PROMPT_VARIABLES


class ClockPlugin:
    name = "clock"          # 身份（卸载、台账都用它）
    inject = ()             # 依赖的服务名；不依赖就留空

    def setup(self, ctx: Context):
        ctx.on(                          # 事件订阅：装载期订阅，卸载自动退订
            PROMPT_VARIABLES,
            lambda value, **kw: {**value, "time": "2026-10-13T00:00:00+08:00"},
        )
        ctx.provide("clock.ready", True)  # 服务：注册即 effect（卸载自动撤销）
        # 也可以返回一个显式撤销函数（做 ctx 管不到的清理，如外部注册表）
```

装载：`ctx.load_plugin(ClockPlugin())`；或走进 `boot(plugins=[...])`。
卸载：`ctx.unload_plugin("clock")`——服务、订阅一起回卷。
