# 0054 装配单支持 YAML：数据与代码白名单分开

- 日期：2026-10-19
- 阶段：P4_Coding（M2 收尾 / M3 起步）
- 状态：已采纳

## 背景

0042 把"装配单是数据"定成了基座立场，但当时只有 Python 字面量一份实现
（`manifests/minimal.py`），并在注释里留了一个待定项：

> 等 M4 出现三个产品形态、需要"改配置不改代码"时再定文件格式。

用户现在要求：**在 `manifests/` 里做一个符合 launcher + 装配单的 YAML 最小应用**。
也就是要验证"同一份 core + 启动器，换一份数据就换产品"这条主张在文件格式
上同样成立。

## 决策

**YAML 只承载数据；插件工厂的白名单留在代码里。**

    manifests/minimal.yaml   条目树：id / name / role / config / enabled / children
    manifests/load.py        读文件 → 替换部署变量 → 形状校验 → list[dict]
    manifests/minimal.py     catalog()：name → 工厂的白名单
    core/launcher.boot()     不变：仍然接收条目列表，不读文件

### 为什么 launcher 不读 YAML

引导的职责是"建 ctx → 挂 loader → 装单子 → 产品政策"，文件 I/O 和 YAML
依赖不该进 core（0042：启动器不读文件、不碰环境变量）。`app.py` 负责
"读哪种文件、填哪些部署变量"，然后照旧调用 `boot()`。

### 为什么 YAML 不能决定"装哪个工厂"

装配单里的 `name` 必须在 `catalog()` 里。如果 YAML 能写 Python 路径或任意
工厂，它就变成了可执行配置——热插拔与安全边界都失去意义。这是
"条目是数据、目录是代码"的分工。

### 占位符

YAML 里可写三种字符串占位符：

    ${workspace} / ${sessions} / ${sandbox} / ${approval_policy}
        由 `app.py` 从命令行参数填入（部署决策）
    ${env:NAME}                 环境变量，缺失 → fail loud
    ${env:NAME:-默认值}          环境变量，缺失时用默认值

整串是一个占位符时保留原类型（`${count}` → int）；否则按字符串拼接。
未知变量、缺失的环境变量都在**启动前**报错——拼错的变量名不该静默变成
空串。

### YAML 版的模型路由

Python 版离线装配单把剧本 `transport` 写成函数，YAML 表达不了函数。因此
YAML 版改用 `llm-fake` 插件做离线路由，同时装一条真实的 `llm-provider`
（`deepseek`，密钥来自 `DEEPSEEK_API_KEY`）。两条路由并存，用
`--provider fake|deepseek` 选择；`--manifest` 与 `--realtime` 因此互斥。

## 备选与排除理由

**A. 让 launcher 直接接受文件路径**。最省事，但把 YAML 依赖、文件 I/O、
部署变量都塞进 core，违背 0042 的"启动器不读文件"。放弃。

**B. 用 Python 文件当装配单（现状）**。能表达函数与任意对象，但"改配置
要改代码"正是用户点名要避免的；而且 Python 装配单没有安全的形状校验。
保留为离线默认，YAML 作为第二个消费者。放弃"只留一种"。

**C. YAML 里允许 `!factory plugins.xxx` 标签**。等于把可执行代码放进配置，
白名单形同虚设。放弃。

**D. TOML / JSON**。TOML 表达嵌套条目树很别扭；JSON 不能写注释。YAML 是
配置文件的常见选择，PyYAML 已在环境里。放弃。

## 后果

- `manifests/load.py`：`load_manifest(path, variables=...)`，`ManifestFileError`
  带文件与位置；`yaml.safe_load`，不构造任意对象；
- `manifests/minimal.yaml`：与 Python 版同一形状（26 个条目），多一条
  `llm-fake` 路由；
- `app.py` 新增 `--manifest PATH`；`--manifest` 与 `--realtime` 互斥；
- `catalog()` 增加 `llm-fake`（YAML 无法表达 transport 函数时的离线后端）；
- `llm-provider` 的 `base_url` 现在接受服务根地址或完整端点：根地址 / `/v1`
  自动补 `/chat/completions`，已有路径原样使用；
- `tests/test_manifest_yaml.py` 13 个用例：形状一致、部署变量、占位符
  fail loud、未知插件名在启动期被拦、YAML 装出来的世界跑通一个真 turn；
- 全量 500 用例通过；YAML 单子经真实 DeepSeek 跑通 `calc` 工具往返
  （steps=2）。

## 相关

- [0042](0042-p4-base-boundary.md)（基座边界、启动器不读文件）、
  [0050](0050-minimal-app.md)（最小应用）、
  [0053](0053-plugin-dependency-direction.md)（插件依赖方向）。
