# _03_Prompt_Trace —— 装配来源追溯与重建断言

P2 第 3 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> `--dump-prompt` 打印每个 section 的来源；并断言「渲染出的提示词能由 日志 + 装配器 重建」。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/prompt/ | _02 的动态装配 | ★ 来源追溯 + 重建断言 |
| harness/cli.py | chat / run / list / show / fork | 加 `run --dump-prompt` |

## 0) 现状基线

```bash
cd P2_Coding/_03_Prompt_Trace
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `docs/subsystems/system-prompt.md`（可追溯性一节）

**做**：
- `--dump-prompt`：打印装配顺序、每个 section 的内容与**来源**；
- 重建断言：`日志 + 装配器 == 当时渲染出的提示词`。

**验收**：
- dump-prompt 可读、含来源信息
- 重建断言通过
- 快照稳定；基线 22 用例仍全绿

**决策记录**：`docs/decisions/0008.md`。

## 2) 运行方法

```bash
cd P2_Coding/_03_Prompt_Trace
python -m pytest -q
python -m harness run "帮我算 1234*56.78" --dump-prompt
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

M4 完成。下一阶段 `_04_Filesystem_Seam` 进入 M5 能力接缝。
