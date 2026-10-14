# _01_Self_Plugin_Tools —— 自扩展工具：模型操作自身插件

P3 第 1 阶段（对应 [PLAN.md](../PLAN.md)）。**本阶段只加一个机制**：

> 给模型三件"插件管理"工具（`list_plugins` / `load_plugin` / `unload_plugin`），
> 让它能查看并装卸自己所在进程的插件——白名单限定、装卸需审批、fail-closed。

**状态：🚧 起步中（目录已从 `mini_harness/` 基线复制，机制尚未实现）**

## 本阶段自包含

本目录从 P3 基线 `mini_harness/`（v0.3，插件化九插件形态）**原样复制**起步——
这是 P3 沿用 P2 的组织方式：每个阶段自包含，从上一阶段完成态复制，只加一个机制。

| 位置 | 角色 | 本阶段将做什么 |
|---|---|---|
| `plugins/self_tools.py`（待建） | ★ 本阶段新增的机制 | 三件自扩展工具 + 白名单 + 审批接线 |
| `plugins/` / `components/` / `kernel/` | 基线（复制自 `mini_harness/`） | 未改动 |
| `chat.py` / `webui/` / `tests/` | 入口与测试 | 工具面里出现新工具；测试见下 |

## 0) 现状基线（起步态）

```bash
cd P3_Coding/_01_Self_Plugin_Tools
python -m pytest -q        # 68 个基线用例全绿（与 mini_harness 一致）
```

## 1) 本阶段目标

**做**（开工前细化成验收清单）：

- 三件工具：`list_plugins`（列台账：名字/状态/inject/提供什么）、
  `load_plugin`（按名装载白名单里的插件）、`unload_plugin`（按名卸载）；
- **白名单**：只有预先批准的名字可装卸（模型不能凭空装任意代码）；
- **审批**：装卸与写文件、跑命令同级（`needs_approval=True`），默认 fail-closed；
- **可观测**：每次装卸在事件流与会话日志里留痕（`plugin/loaded` 等事件 + 工具结果）。

**验收（要点，开工时细化）**：

- [ ] 模型列出的插件台账与 `ctx.plugins` 一致
- [ ] 白名单内装卸生效：工具面/服务即时变化，卸载后依赖者退位
- [ ] 白名单外直接拒绝（结构化错误，不抛异常）
- [ ] 需审批：拒绝路径下不改动任何东西
- [ ] 基线 68 用例仍全绿；门禁绿

## 2) 运行方法

```bash
cd P3_Coding/_01_Self_Plugin_Tools
python -m pytest -q
python chat.py --ask "列出现在装了哪些插件"    # 离线一个 turn
python chat.py                              # 离线交互
python chat.py --web                        # 界面（同基线）
python chat.py --rpc --listen 8771          # RPC（同基线）
./run.bat                                   # 启动器（同基线）
```

## 3) 继承自基线的能力（不重复实现）

本目录的 `kernel/` `components/` `plugins/` `webui/` 与 `mini_harness/` 基线**逐字节一致**
（唯一差异是测试文件名：`test_s01_*`，避免与基线在全量跑时同名冲突）。
基线能力清单与阅读路线见 [../mini_harness/README.md](../mini_harness/README.md)：
九插件（llm / fs / proc / tools / sessions / prompt / agent / webui / rpc）、
8 件工具面、事件溯源日志、界面与 RPC 两个服务插件。

## 4) 完成后的去向

`_02_Streaming`（PLAN.md）：流式输出——供给端出 chunk、以 `llm/stream` 事件流出、
循环聚合为完整响应并支持取消。

---

> 目录说明：`_01` 是开发位置；`mini_harness/` 是基线快照（里程碑处重新生成）。
> 阶段定位与路线见 [PLAN.md](../PLAN.md) 与 [../README.md](../README.md)。
