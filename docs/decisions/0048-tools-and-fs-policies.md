# 0048 工具面：tools-admin + 六个工具插件 + 两层文件策略

- 日期：2026-10-16
- 阶段：P4_Coding（M2）
- 状态：已采纳

## 背景

用户给定的结构：

> tools-admin —— base，必要依赖：fs-sandbox / fs-observation-policy / tool-calc /
> tool-fs / tool-shell / tool-fs-search / tool-web-search / tool-ask-user /
> proc-service；提供插件 tools 的 list、安装、卸载、禁用、管理等服务，提供
> 插件的运行管线
>
> fs-sandbox：真实读写隔离沙箱，四种模式（全部审核 / 变更确认 / 自主确认 /
> 完全访问）
> fs-observation-policy：修改文件前必须先读
> tool-math（calc / math）、tool-fs（read_file / list_files / write_file /
> edit_file）、tool-shell（shell）、tool-fs-search（glob / grep）、
> tool-web-search（web_search / web_fetch）、tool-ask-user（ask_user，至少两个
> 选项 + 自定义输入）
> proc-service —— base，必要依赖：proc-local-git-bash / proc-local-posix /
> proc-scripted

## 决策

### 一、三层：约定 / 后端 / 工具

以文件为例（进程与搜索同构）：

    约定    fs（base）              读写 + 边界 + 策略插座（fs/approve）
    策略    fs-sandbox             按模式决定要不要审批
            fs-observation-policy  已存在的文件必须先读过才能改
    工具    tool-fs                只做 schema + 转发，不做 I/O

**工具插件不实现 I/O**（照 dsh 的分法）：这保证边界只有一个强制点
（`fs` 服务里的 `workspace.resolve()`），沙箱与读后写都是**策略插件**，
装卸它们不改工具、不改 fs 服务。

同理，`proc-service` 是约定，三个后端是能力插件；`tool-shell` 只转发。

### 二、`tools-admin` 是**唯一**的工具服务

初版曾拆成 `tools`（注册表 + 管线）与 `tool-admin`（管理面）两个服务——这是
**多余的**：三者共用同一份状态（注册表），拆开只是让消费方各拿一个门面。
按用户定义改回一个：

    ctx.require("tool-admin").register(...)   工具插件登记
    ctx.require("tool-admin").specs()         agent-loop 拿模型可见工具表
    ctx.require("tool-admin").execute(...)    agent-loop 执行
    ctx.require("tool-admin").snapshot()      管理面（按 owner 分组）
    ctx.require("tool-admin").install/...     管理动作

插件名 `tools-admin`，注册表 / 管线 / 管理动作都在 `ToolsAdminService` 上。

`tools-admin` 的九个依赖全部写成 `requires`：缺任何一个，工具面就是残缺的。
装配期 fail loud，而不是等到模型调用时才 `TOOL_NOT_FOUND`。`requires` 不驱动
生命周期，所以不会与"工具插件依赖 tool-admin 服务"形成等待环。

### 三、管线与四个插座

    validate → tools/pre-execute（allow / deny / ask）
             → tools/execute（包住调用：超时/重试/记账）
             → tools/post-execute（检查 / 替换结果）
             → tools/result（只读观测）

- 参数校验失败 → **普通错误结果**（`TOOL_BAD_ARGUMENTS`），不是异常；
- 工具内部抛错 → `TOOL_EXCEPTION` 结果（不炸整个 turn）；
- 超时 → 在独立线程里执行，超时返回 `TOOL_TIMEOUT`；
- `needs_approval` 的工具走审批通道，没有通道时 **fail-closed**。

**审批协议统一成 `ask(request)`**：`components.fs` 与 `components.tools` 用
同款形状，一个审批服务可以同时服务文件与工具（这条是实测出来的——两套签名
会让同一个 policy 对象不可复用）。

### 四、fs 沙箱的四种模式

| 模式 | 读 | 写 / 编辑 |
|---|---|---|
| `all` | 审批 | 审批 |
| `changes`（默认） | 放行 | 审批 |
| `risky` | 放行 | 有风险时审批；**同一会话批准过的相同请求不再问** |
| `free` | 放行 | 不审批（工作区外仍被 workspace 拦住） |

`risky` 的"有风险"默认判据 = **覆盖已存在的文件**；可以换判据。

### 五、`proc-service` 与三个后端

- `proc` 只做后端选择与转发；没有可用后端时返回 `NO_BACKEND` 错误结果；
- **Git Bash 后端**（Windows 默认）：参数列表直启、`--noprofile --norc`、
  UTF-8 环境变量、解码 UTF-8 → GBK → replace、CRLF 归一、超时杀进程树；
- **POSIX 后端**：`sh -c` + `start_new_session`；
- **剧本后端**：离线测试用。

**方言检查在执行前**：命令看起来是 cmd / PowerShell 语法时直接拒绝并给等价
写法提示——**不自动翻译**（翻错可能造成破坏）。这条默认只在真实后端上生效
（剧本后端 `dialect="unknown"`，不拦）。

### 六、六个工具插件的边界

| 插件 | 工具 | 备注 |
|---|---|---|
| `tool-math` | `calc` / `math` | `calc` 用 AST 白名单求值，**不用 eval** |
| `tool-fs` | `read_file` / `list_files` / `write_file` / `edit_file` | 全部走 fs 服务 |
| `tool-shell` | `shell` | 需审批；方言检查；超时杀树 |
| `tool-fs-search` | `glob` / `grep` | 遍历走 fs 服务；结果有上限 |
| `tool-web-search` | `web_fetch` / `web_search` | 只允许 http/https；搜索端点未配置时 fail loud |
| `tool-ask-user` | `ask_user` | 至少两个选项；无通道时 fail loud（不编造答案） |

`list_tools` / `install_tool` 留在 `tools-admin` 底座里（署名 `tools`）——
"管理工具的工具"不再额外插件化，否则管理链会无限延伸。

## 实测踩到的三个坑（已修）

1. **`Popen(cwd=...)` 不能用 POSIX 路径**：传 `/c/Users/...` 得到
   `WinError 267`。bash 启动后会自己把 cwd 呈现成 `/c/...`，所以不要转换。
2. **工具返回错误状态必须是 `ToolResult`**：`shell` 一开始把 proc 的错误
   包装成普通 dict，管线把它当成功。方言写错也显示 `ok` —— 这正是"错误被
   吞掉"的典型形态。
3. **泛型/联合注解**：`from __future__ import annotations` 下
   `list[str] | None` 是字符串，schema 推导要剥掉 `| None` 与泛型参数，
   否则参数会被当成 string 拒绝。

## 后果

- 工具面现在 13 件工具（2 管理 + 6 插件共 11 件业务工具）；
- `fs` 的审批是**策略插件**：不装沙箱插件就没有额外审批，但工作区边界永远在；
- 超时是"背压"（独立线程 + join timeout）：工具自己不守时也不会卡死 turn，
  但**不能强制终止**已跑起来的工具函数——真隔离要靠进程（P5）；
- `web_search` 需要一个 SearXNG 兼容端点；未配置时明确报错。

## 相关

- [0042](0042-p4-base-boundary.md)（策略插座、最小基座）、
  [0047](0047-sessions-workspace-project.md)（工作区边界）、
  [0046](0046-prompt-plugins.md)（工具说明进提示词）。
