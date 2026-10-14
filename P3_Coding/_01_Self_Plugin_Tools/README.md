# _01_Self_Plugin_Tools —— 自扩展工具：模型操作自身插件

P3 第 1 阶段（对应 [PLAN.md](../PLAN.md)）。**本阶段只加一个机制**：

> 给模型三件"插件管理"工具（`list_plugins` / `load_plugin` / `unload_plugin`），
> 让它能查看并装卸自己所在进程的插件——白名单限定、装卸需审批、留痕。

**状态：✅ 已完成（2026-10-14）**

## 本阶段做了什么

新增两个插件，其余一切照旧（目录自包含，从基线 `mini_harness/` 复制起步）：

| 文件 | 角色 | 内容 |
|---|---|---|
| `plugins/self_tools.py` | ★ **本阶段的机制** | `SelfToolsService`（list / load / unload，结构化错误）+ `SelfToolsPlugin`（提供 `self-tools` 服务并注册三件工具） |
| `plugins/clock.py` | ★ 示例可装卸插件 | `ClockPlugin`：装载它多一个 `now` 工具与 `clock` 服务，卸载即回卷——用来演示"装载即扩展" |
| `chat.py` | 入口 | 新增 `--self-tools` 开关（离线/真实两种组合都支持） |
| `plugins/boot.py` | 装配 | `boot_offline/boot_realtime` 新增 `self_tools=False` 参数 |
| `plugins/services.py` / `events.py` | 词汇 | 新增 `SELF_TOOLS_SERVICE` / `CLOCK_SERVICE`；新增 `plugin/loaded`、`plugin/unloaded` 广播事件 |

### 三条安全设计（本阶段的核心）

1. **可装载 = 白名单里有名字**：catalog 是 `插件名 → 工厂函数` 的显式映射
   （默认 `clock` / `webui` / `rpc`）。模型给的是**名字**；本实现没有"从磁盘发现
   插件"这回事，所以它无法凭空引入新代码——装什么，装配方说了算。
2. **可卸载 = 另一份白名单**（默认只有 `clock`）：卸载还在被使用的能力会破坏
   当前会话——最典型的是卸载 `webui`（它正是网页对话的传输通道，一卸连接就断）。
   要放开哪些名字，由装配方显式决定。
3. **装卸需审批**（`needs_approval=True`，与写文件、跑命令同级）：策略没配或人
   拒绝时 **fail-closed**，什么都不发生；`list_plugins` 只读、不打扰用户。

### 可观测

- 每次装卸发 `plugin/loaded` / `plugin/unloaded` 内核事件（供订阅者/RPC 跟随）；
- 工具调用本身进会话日志（`tool/result` 事件）——**模型动过什么有据可查**；
- 返回值如实说明"新增/移除了哪些服务与工具"，并提示新工具**从下一轮开始**可用。

## 验收（对照 PLAN.md）

- [x] 模型列出的台账与 `ctx.plugins` 一致（同源，永不发岔）
- [x] 白名单内装卸生效：装 `clock` → 工具面多 `now`、服务多 `clock`；卸载 → 全部回卷
- [x] 卸载后依赖者退位（换 `fs` 时 `tools` 重建，`self-tools` 随之退位/重装）
- [x] 白名单外直接拒绝（结构化错误、不抛异常、无副作用）
- [x] 需审批：拒绝路径下不改动任何东西；`list_plugins` 不触发审批
- [x] 基线 68 用例仍全绿；本阶段 20 个新用例；门禁绿（ruff + pytest）

## 运行方法

```bash
cd P3_Coding/_01_Self_Plugin_Tools
python -m pytest -q                                  # 88 个用例（基线 68 + 本阶段 20）

# 离线：让模型自己装卸插件
python chat.py --self-tools --fake --ask "列出现在装了哪些插件？"
python chat.py --self-tools                          # 交互：可以说"把 clock 装上，然后用它报个时"

# 真实 API（读项目根 .env 的 key）
python chat.py --self-tools --realtime
python chat.py --self-tools --realtime --web          # 界面里也能看它装卸

# 不装 --self-tools 时，行为与基线完全一致
```

## 真实运行的例子（离线剧本驱动，实测输出）

```
step1 list_plugins   ✓ 台账 8 项 + loadable/unloadable + 工具面
step2 load_plugin    ✓ {"loaded": "clock", "services_added": ["clock"], "tools_added": ["now"]}
step3 now            ✓ {"now": "2026-10-14T10:59:08", ...}      ← 上一轮刚装的工具，这一轮就能用
step4 unload_plugin  ✓ {"unloaded": "clock", "tools_removed": ["now"]}
最终工具面回到 11 件；clock 服务已撤销
```

## 完成后的去向

`_02_Streaming`（PLAN.md）：流式输出——供给端出 chunk、以 `llm/stream` 事件流出、
循环聚合为完整响应并支持取消。

---

> 目录说明：`_01` 是开发位置；`mini_harness/` 是基线快照（里程碑处重新生成）。
> 阶段定位与路线见 [PLAN.md](../PLAN.md) 与 [../README.md](../README.md)。
