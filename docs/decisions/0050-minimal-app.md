# 0050 最小应用：launcher + 装配单 → 一个 AI 服务

- 日期：2026-10-16
- 阶段：P4_Coding（M2 收尾 / M4 预演）
- 状态：已采纳

## 背景

用户要求：**从外部看，启动一个 AI 服务应该只有两样东西——launcher + 装配单。**

之前的状态是：`boot()` 只有测试和 demo 在用，装配单还是写在 Python 代码里的
`list[dict]`，没有一个真正的入口脚本。这次把它落成可运行的形态。

## 决策

### 一、`project` 升格为"项目装配面"

`project.requires` 增加 `agent-admin`（它再传递拉入 agent / agent-loop）。
理由：没有 agent 的 project 只是一个"带工作区的会话库"，不是 AI 服务。

**`project.open()` → `project.start()`**，并且只走 `agent-admin`。这是实测
挖出来的 bug：

```
project.open("s1")            → 写了 session/start（空提示词）
agent-admin.create("s1")      → sessions 发现会话已存在，直接复用
                                → 渲染好的提示词没有写进去
```

结果日志里留下一条空提示词的 `session/start`。修法是**入口唯一**：会话由
agent-loop 在 create/resume 时建立并写入渲染后的提示词，project 只做转发。

`start()` 的三种情况：

    内存里已有    → resume
    磁盘上有历史  → resume
    都没有        → create

### 二、装配单是数据（`manifests/minimal.py`）

```python
manifest(workspace=..., sessions=..., sandbox_mode=..., replies=...)
catalog()    # name → 工厂
```

25 个条目，按注释分五组：基础 / 工具面 / 策略与后端 / agent 与入口。
参数是**部署决策**（目录、沙箱模式、剧本），不是插件逻辑。

**为什么还是 Python 字面量**（0042 的待定项）：第一个消费者，零依赖、
测试可直接 import、写错字段装配期就报。等 M4 出现三种产品形态、需要
"改配置不改代码"时再定 YAML/TOML。

### 三、离线模型用**真的适配器** + 假传输

一开始装配单里用的是 `llm-fake`，但 `llm` 的插件级依赖是 `llm-provider`
（0045 决定"假适配器不算产品有了一条模型路由"）。所以改成：

    llm-provider（真适配器：方言、参数、校验全走真实路径）
        └─ transport = _offline_transport(剧本)

接真实模型时只换 `base_url` / `api_key` / `transport` 三行，其余不动。
**装配单是数据，换模型不改代码**——这一条现在有可运行的证明。

### 四、`app.py` 是入口，暂时 `product=False`

```bash
python app.py                          # 交互式（离线剧本）
python app.py --realtime               # 交互式（真实 DeepSeek）
python app.py --realtime --yes --ask "…"  # 真实模型 + 自动批准工具
python app.py --dump                   # 只装配并打印插件树
python app.py --sandbox free
```

**为什么不是 `product=True`**：产品政策要求"至少一个 `role=application`
的条目"，而现在还没有 `cli` 插件——`app.py` 自己就是入口，但它不在树上。
M4 把 `cli` 做成应用化插件之后，改成 `product=True` 即可（有一条测试
钉住"现在按产品启动会被正确拒绝"）。

### 五、真实模型装配单（`manifests/realtime.py`）

与 `minimal` 的**唯一差别是模型后端那一行**：

    minimal   llm-provider + 本地剧本 transport
    realtime  llm-provider + 真实 base_url，密钥走 api_key_env

凭证只从环境变量取（`app.py` 启动时加载项目根 `.env`），装配单与代码里
都不出现密钥。端点/模型从 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 读，
没配就用默认值。

### 六、审批通道（`approval-terminal`）

真实模型会主动调 `shell` / `write_file`，而工具审批默认 fail-closed。
补一个终端审批插件：

    policy="ask"    交互式 y/n（非 TTY → 拒绝）
    policy="allow"  `--yes` 自动批准
    policy="deny"   无人值守

**实测踩到的 bug**：`tools-admin` 与 `fs-sandbox` 在 `setup()` 时抓了一份
`approval` 服务，而审批插件排在它们之后装载 → 后来装上的审批通道永远不
生效（`--yes` 给了 allow，shell 仍然全部 denied）。修法是**用时现取**
（`_LazyApproval`），并加了两条测试钉住"审批后装也生效"。

## 验收（全部机器可查）

| 验收 | 测试 |
|---|---|
| 装配单 25 个条目全部 active | `test_manifest_assembles_all_entries` |
| 一个真 turn 跑通，`session/start` 里有提示词 | `test_one_real_turn_through_the_manifest` |
| 重启后恢复并续写 | `test_session_survives_restart` |
| 模型调工具 → 回灌 → 最终回答 | `test_model_can_call_a_tool` |
| 没有应用化插件时产品政策拒绝 | `test_product_policy_rejects_manifest_without_application` |
| 审批通道后装也生效 | `test_approval_installed_after_tools_still_works` |

**真实 API 端到端（本机实测，DeepSeek `deepseek-flash`）**：

| 场景 | 结果 |
|---|---|
| 单轮问答 | `[done] 你好`，1741 tokens |
| 模型自主调 `calc` | 日志里有 `tool/call → tool/result(391)`，2 步 |
| 读工作区文件 | `list_files` → `read_file`，内容正确 |
| 执行命令 | `shell pwd` → `/d/project/Harness-Learning/P4_Coding/workspace` |
| 写文件再读回 | `write_file` → `read_file`，`notes.txt` 落盘内容一致 |
| 方言写错 | `dir /b` 被拒并收到等价写法提示 |

手动验证（本机实测）：

```
$ python app.py --dump
条目                    角色          状态       插件 / 说明
app                   -           group    （分组）
app:workspace         base        active   workspace
...
app:project           base        active   project
agent 模式： default, quick, careful

$ python app.py --session test1 --ask "你好"
  steps=1 tokens=2
[done] （离线模型）你好。

$ python app.py --session test1 --ask "再说一句"   # 恢复同一会话
  steps=1 tokens=5
```

## 后果

- 现在起一个服务只需要 `app.py` + 一份装配单（25 行数据）；
- `workspace/` 与 `sessions/` 默认落在项目目录下（`.gitignore` 应忽略）；
- 真实模型接入是换装配单里的三行配置，不碰代码；
- M4 的 `cli` / `web` / `sdk` 会把 `app.py` 里的 REPL 逻辑搬成插件，
  那时 `app.py` 只剩 `boot(...)` + 一次 `runtime.close()`。

## 相关

- [0042](0042-p4-base-boundary.md)（launcher 是唯一的组合根）、
  [0045](0045-llm-capability-group.md)（llm-provider 是插件级依赖）、
  [0049](0049-agent-trio.md)（agent 三件套）。
