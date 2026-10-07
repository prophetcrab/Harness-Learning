# _01_Prompt_Assembly —— M4：系统提示与上下文装配

P2 第一个阶段，对应学习计划 **M4**。目标：**提示词是可组合、可追溯、可重建的产物**，
不再是散落在各处的字符串拼接。

## 本阶段是自包含的

本目录带一份**完整的 `harness/` 副本**（从 P1 `_05` 收敛来的基线 + 四个阶段的占位子包）。
阶段之间不共享代码：你在本目录里写 M4 的实现，需要哪个阶段就抄哪个阶段的目录。
好处是每个阶段都能单独跑、单独读；代价是会重复（这是刻意的取舍）。

`harness/` 里与本阶段相关的部分：

| 子包 | 现状 | 本阶段要做的 |
|---|---|---|
| `harness/prompt/` | 占位（只有 `__init__.py`） | ★ 写 section 注册表 + 变量插值 + 装配器 |
| `harness/agent/loop.py` | 每 step 用固定 `system_prompt` | 改为每 step 调用装配器渲染 |
| `harness/mini.py` | 装配时接收 `system_prompt` 字符串 | 改为接收/构造装配器 |
| `harness/session/` | 已有事件日志 | `system/message` 进日志（现在是 `session/start` 携带） |

## 0) 现状基线（动手前先确认它绿的）

```bash
cd P2_Coding/_01_Prompt_Assembly
python -m pytest -q        # 22 个用例：M1–M3 组装基线，必须全绿
```

这 22 个用例是**行为基线**：你改 `prompt`/`agent`/`mini` 时不能把它们弄坏
（`demo.py` 同理）。基线不稳就别往上加东西。

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/core/system-prompt/src/index.ts`（section 注册与渲染）
- `packages/context/`（workspace 指令、时间上下文这类运行时注入）
- `docs/subsystems/system-prompt.md`

**做**：
- `harness/prompt/`：**section 注册表**——有序、支持作用域覆盖（呼应铁律 #4 分层遮蔽）；
- **变量插值**：`{{cwd}}` / `{{platform}}` / `{{time}}`；
- 运行时上下文**每 step 渲染**，渲染结果作为 `system/message` 进会话日志；
- **装配断言**：渲染出的提示词必须能由「日志 + 装配器」重建；
- CLI：`python -m harness run --dump-prompt` 打印装配过程与每个 section 的来源。

**验收**：
- [ ] 快照测试稳定（同一状态渲染两次结果一致）
- [ ] 改 `cwd` 只影响引用它的 section，其他 section 不动
- [ ] 新增一个 section 不影响已有部分
- [ ] 渲染结果可由「日志 + 装配器」重建（断言）
- [ ] 门禁全绿；基线 22 用例仍全过

**决策记录**：`docs/decisions/0006-prompt-assembly.md`（为什么用 section 注册表而非模板引擎）。

## 2) 运行方法

```bash
cd P2_Coding/_01_Prompt_Assembly

python -m pytest -q                      # 基线测试（改完应保持 22+ 全绿）
python demo.py                           # 离线端到端回归
python -m harness run "帮我算 1234*56.78" --fake
python -m harness run "..." --dump-prompt   # 本阶段新增：打印装配过程
python -m harness.webui.server           # 可视化页面（真实 API）
./run.bat chat --fake                    # 一键（双击 run.bat 即离线 demo）
```

## 3) 完成后的去向

M4 的产物是"提示词装配器"。下一阶段 `_02_Capability_Seams` 会从本阶段目录**复制一份**
作为起点，在其上做能力接缝（M5）。
