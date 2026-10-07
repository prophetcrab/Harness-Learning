# _02_Prompt_Context —— 变量插值与运行时上下文

P2 第 2 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> section 里的 `{{cwd}}`/`{{platform}}`/`{{time}}` 在**每 step 渲染**时求值，渲染结果作为 `system` 消息进日志。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/prompt/ | _01 的注册表 + 装配器 | ★ 加变量插值 + 运行时上下文 |
| harness/agent/loop.py | 每 step 用固定提示 | 每 step 渲染运行时上下文 |
| harness/session/ | system 只在 session/start | 每次渲染写 system/message 事件 |

## 0) 现状基线

```bash
cd P2_Coding/_02_Prompt_Context
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/context/`（workspace 指令、时间上下文）
- `core/system-prompt` 的运行时渲染部分

**做**：
- 实现变量插值 `{{cwd}}` / `{{platform}}` / `{{time}}`；
- 每 step 渲染运行时上下文，把结果作为 `system/message` 事件写进会话日志；
- 改 `cwd` 只影响引用它的 section。

**验收**：
- 三种变量的插值正确
- 改 cwd 只影响对应 section，其它 section 不动
- system/message 进日志且可投影
- 快照稳定；基线 22 用例仍全绿

**决策记录**：`docs/decisions/0007.md`。

## 2) 运行方法

```bash
cd P2_Coding/_02_Prompt_Context
python -m pytest -q
python -m harness run "现在几点？" --fake
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

`_03_Prompt_Trace` 加「来源追溯」与「可由日志 + 装配器重建」的断言。
