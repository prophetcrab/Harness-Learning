# _01_Prompt_Sections —— 提示词 section 注册表与装配

P2 第 1 阶段（对应学习计划 **M4**）。本阶段**只加一个机制**：

> 把「一个写死的 system_prompt 字符串」升级成**有序的 section 注册表 + 装配器**——提示词是组装出来的，不是手写的。

## 本阶段自包含

本目录带一份**完整的 `harness/` 副本**（P2 约定：阶段之间不共享代码，后一阶段从上一阶段的
`harness/` 复制起点）。`harness/` 里与本阶段相关的部分：

| 位置 | 现状（本阶段起点） | 本阶段要做的 |
|---|---|---|
| harness/prompt/ | 占位（只有 __init__.py） | ★ section 注册表 + 装配器 |
| harness/mini.py | 接收 system_prompt 字符串 | 改为接收 section 列表 / 装配器 |
| harness/agent/loop.py | 用固定 system_prompt | 每 step 向装配器取 system 提示 |

## 0) 现状基线

```bash
cd P2_Coding/_01_Prompt_Sections
python -m pytest -q        # 22 个用例（M1-M3 组装基线），必须全绿
```

## 1) 本阶段目标

**读**（dsh 参考）：
- `packages/core/system-prompt/src/index.ts`（section 注册与渲染）
- `docs/subsystems/system-prompt.md`

**做**：
- `harness/prompt/`：**有序** section 注册表，支持**作用域覆盖**（呼应铁律 #4「注册表分层 + 遮蔽」）；
- 装配器：按注册顺序把 section 拼成完整 system 提示词；
- 快照测试：同一组 section 渲染两次结果一致。

**验收**：
- section 按注册顺序装配
- 新增/删除一个 section 不影响其它 section
- 作用域覆盖能遮蔽同名 section
- 基线 22 用例仍全绿；门禁绿

**决策记录**：`docs/decisions/0006.md`。

## 2) 运行方法

```bash
cd P2_Coding/_01_Prompt_Sections
python -m pytest -q
python -m harness run "帮我算 1234*56.78" --fake
python demo.py
./run.bat                    # 双击即离线 demo；run.bat chat --fake 交互对话
```

## 3) 完成后的去向

把「静态 section」升级为「动态上下文」：下一阶段 `_02_Prompt_Context` 加变量插值与运行时渲染。
