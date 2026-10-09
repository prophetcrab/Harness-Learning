# 0013 profile 分层：装配变成数据，patch 整块替换不合并

- 日期：2026-10-09
- 阶段：P2_Coding/_08_Profile_Layers（学习计划 M6 第二步）
- 状态：已采纳

## 背景

`_07` 立了插件与 effect——装配动作可回卷了，但**装配本身还是代码**：
`load_plugins(ctx, [make_fs_plugin(LocalFS(ws)), ...])` 写在 chat.py 里。
M6 的目标是"能力组合从代码变成配置数据"：同一个底座，不同环境（开发/生产）
要换实现组合，改代码就得改文件、跑测试、走 review；dsh 的做法是 profile：
配置树是数据，多份 profile 是叠在底座上的补丁，换实现 = 换一行补丁。

本阶段要立的机制（README 的四条验收）：有序层 + 按 id patch；
叠加顺序 base → profile → user → CLI；两 profile 产出不同且可读的树；
"换 LLM provider 只改一行"。

## 决策

**1) 行是身份，name 是实现**（`config/rows.py`）：配置树是**有序的行**，每行
`id`（patch 的锚点，全树唯一）/ `name`（去工厂注册表查实现）/ `config`（参数）/
`disabled`（留在树里但不激活）。把"身份"与"实现"分开，patch 才能精确指向某一行去
换它的实现——"换 provider 只改一行"是这条分离的直接推论。

**2) patch 整块替换，不做字段合并**（`config/patch.py`）：patch 行给了 `config`
就整个换（`config: {}` 即清空）；没给就沿用。dsh 明说 "a patch replaces the
targeted row's whole `config` rather than merging into it"，并且为此宁可让每层
"restate its complete configuration"。**为什么反直觉地不做 merge**：字段级合并让
"这行最终是什么"变成跨文件脑内求并集——排查配置问题最贵的就是这个；整块替换保证
**每一行的最终值只来自"最后一个碰过它的层"**，一眼可读。代价是多写几行，接受。

**3) 替换保持原位，insert 追加末尾**：骨架（行的顺序与存在性）由 base 决定，
patch 只换血肉；新能力用 insert 加在末尾。行序 = 激活顺序，被依赖的行（fs/subprocess）
在前、消费者（toolbox）在后——顺序错了会在装载时 fail loud（`ctx.require` 缺槽位）。

**4) 层序 base → profile → user → CLI**（`config/profiles.py`）：直接对齐 dsh
profile-boot 的"bundle 层 → profile 自身 → --patch 覆盖层"。`user.patch.yaml`
夹在 profile 与 CLI 之间（个人本地偏好覆盖 profile 默认、但被命令行压过）。

**5) 构造与激活两步分离**（`providers/plugins.py`）：`build_plugins_from_tree`
只**构造**（查工厂、校验参数、new 对象）；`boot_tree` 才装载（`load_plugins`）。
好处：**配置错误（未知 name、非法参数、缺 key）在任何副作用之前全部暴露**；
装载阶段的失败（槽位冲突等）由 kernel 回卷。两个阶段的 fail loud 分属两层，
诊断信息各说各的话（"行 llm 的 name 未知" vs "槽位 fs 已存在"）。

**6) LLM 也纳入配置**：`_04`–`_06` 把 fs/subprocess 做成了接缝，但 LLM 一直由
入口手工选（`--fake` 开关）。本阶段给 LLM 加了 `llm` 槽位与 `llm:fake` /
`llm:deepseek` 工厂，于是"四个能力全部由配置驱动"；`llm:deepseek` 的
model/base_url 解析优先级 = 配置 > 环境变量 > 内置默认（显式解析：行里配了什么
一目了然）。

## 备选与排除理由

1. **config 做字段级深合并**。排除见决策 2：多文件求并集是配置可读性的天敌；
   且"清空某字段"在 merge 语义下无解（得发明 null 语义）。
2. **只有 name 没有 id（patch 按 name 定位）**。排除：同一类实现可能有多行
   （对照实验、多工作区），name 不唯一；且换实现时 name 变了，按 name 定位会
   自相矛盾。id 是稳定身份，这正是 dsh 给每行配 id 的原因。
3. **profile 用 Python 字典而非 YAML**。排除：YAML 是"配置数据"的自然形态，
   非程序员可改；且 dsh 的做法就是 patch YAML（`cordis.patch.yml`）。我们已在
   依赖里有 pyyaml。
4. **patch 时未知 id 静默跳过**。排除：拼错行 id 是最常见的手滑，静默跳过会让
   "我明明改了配置却没生效"成为最难查的一类问题——fail loud（铁律 #8）在配置层
   的收益最大。
5. **把 boot 直接内置到 load_profile（一步到位）**。排除：分离后配置层是纯数据
   （可测试、可 dump、可 diff），激活层才碰对象；`_09` 的 dump-config 也要在
   "不激活"的前提下工作。

## 后果

- 约束：`config/` 是纯数据层（只依赖 yaml）；`providers/plugins.py` 新增对 config
  的导入（`boot_tree` 消费树）——依赖方向 `providers → config`，config 不反向依赖。
  配置行的 name 必须在 `PLUGIN_FACTORIES` 里注册，否则 boot 前 fail loud。
- 收益：**"换 LLM provider 只改一行"成为字面事实**（demo 0b / wiring 测试 /
  真实 API 实测：dev + 一层 CLI 补丁 → `llm:deepseek`，其余行原样）；dev/prod
  两份 profile 的差异可枚举（三行）；配置错误全部 fail loud 且定位到行；
  `--dump-config` 让最终树可见。67 个用例（22 基线 + 45 新增），全项目 648 用例
  全绿；跨 `_01`–`_08` 同进程 488 用例无串味。
- 债务与后续：
  1. **`--dump-config` 尚无"来源层"标注**（这行来自 base 还是 profile？）——
     正是 `_09` 的题目（配置可见 + 错误定位）。
  2. **工厂参数校验较松**（如 `shell_timeout` 用 `float(config.get(...))`，
     非法字符串会抛 ValueError 而非带行号的 ConfigError）；`_09` 的错误定位
     会一并收拢。
  3. **webui 未接配置**（沿袭前几阶段边界）。
  4. **真实 API 冒烟发现的编码边界仍在**（Windows `dir` 的 GBK 输出按 utf-8
     解码乱码），记录在 0012 的债务里，尚未处理。
