# 0047 会话 / 持久化 / 工作区 / 项目

- 日期：2026-10-16
- 阶段：P4_Coding（M2）
- 状态：已采纳

## 背景

用户给定的结构：

> project —— base，必要依赖：workspace / sessions / session-store-jsonl
> workspace：工作区插件，限定 project 的 LLM 工作范围
> workspace-admin：工作区管理插件，编辑管理工作区，添加或删除工作区，返回工作区
> 文件夹位置、当前文件夹大小
> sessions —— base，会话注册、追加事件、投影出消息历史、打开/恢复的入口
> session-store-jsonl：订阅 session/event，写 JSONL、启动时读回、修复被截断的末行

## 决策

### 一、会话内核 vs 持久化：两个插件

| 插件 | 角色 | 职责 | 缺失时 |
|---|---|---|---|
| `sessions` | base | 注册、追加事件、投影、打开/恢复 | harness 不可用 |
| `session-store-jsonl` | capability | 订阅事件写盘、读回、修复末行 | 会话照常跑，只是重启不能恢复 |

拆开的理由：两者的失败模式不同（内存逻辑 vs 磁盘/半行写入），消费者也不同
（内核给 agent-loop / 推流 / 统计用，JSONL 只是持久化的一种）。将来加别的
存储后端，不动 `sessions`。

**接入方式是事件总线**，不是直接调用：

    写：session/event（emit，带 session_id + session_event）
    读：session/load（waterfall，内存里没有时问订阅者要历史）

这样 `sessions` 不反向依赖存储插件——不装 JSONL 也能纯内存运行（有测试）。

### 二、事件形状是跨插件的约定

    session/start     system_prompt + prompt_trace + model（含 workspace）
    turn/start        turn + text
    step/start        step + model
    assistant         content + tool_calls[{id,name,arguments}]
    tool/call         call_id + name
    tool/result       call_id + status + content + duration_ms
    step/end          step + usage
    turn/end          turn + status + steps + final_text
    system/message    变化后的系统提示词（遮蔽前一条）
    compact/checkpoint  压缩声明（投影时折叠）

投影规则：`turn/start` → user；`assistant` → assistant（tool_calls 原样保留）；
`tool/result` → tool 消息（用 `call_id` 关联）；其他事件只作观测。

投影**缓存到下一次追加**（事件变了才重算），且是纯函数
（`project_messages(events)` 可单独测试/重放）。

### 三、JSONL：只追加 + 修复末行

- 一条会话一个文件 `<root>/<safe_id>.jsonl`，每行一个事件；
- 追加后 `flush + fsync`：崩溃最多丢最后一行；
- 读回时跳过坏行（半行 / 非法 JSON / 缺字段），把修复报告放进
  `store.last_repair`（lines_total / ok / broken）；
- session_id 会被清洗（路径分隔符、连续 `..`）——不能让它决定写到哪去。

### 四、workspace：边界就是全部

`workspace` 只做一件事：**限定工作范围**。它不读写文件、不创建目录。

    resolve(path)    相对路径 → 工作区内绝对路径；越界 → WorkspaceError
    contains(path)   是否在界内
    info()           目录 / 文件数 / 总字节 / 人类可读大小

判据用**解析后的真实路径**（跟随符号链接、消掉 `..`），不做字符串前缀比较——
`root/../outside` 前缀看着在界内，解析后其实在外面。

`must_exist=True`（默认）要求目录已存在：工作区是"给人用的目录"，不该由基座
默默创建。

### 五、workspace-admin：只登记，不删文件

    list() / info(name)        多个工作区的目录与大小
    create(name, root)         登记（目录必须已存在；本插件不创建）
    remove(name)               摘登记（**不删目录**）
    set_active(name)           切换当前工作区

**不删目录是刻意的**：真删文件是破坏性操作，应该由人在文件系统里做，或由一个
显式审批过的工具做——管理面不给一条捷径。

### 六、project：三样缺一不可

`project` 不新增能力，只把三个服务绑在一起，并给一个项目视角的入口：

    project.workspace / sessions / store
    project.open(session_id, ...)   打开会话，并在 session/start 里记下工作区
    project.info()                  工作区 / 会话列表 / 存储位置

依赖用 `requires`（插件级）而不是 `inject`：没有工作区就无处干活、没有会话就
无法对话、没有持久化就丢历史——缺一个"项目"就不成立，应该在**装配期** fail
loud。`requires` 不驱动生命周期，避免 project 与三个依赖之间形成等待环。

**注意**：`session-store-jsonl` 是必需依赖，所以 `project` 不能在没有持久化的
配置里使用——这是用户给的定义，照做；纯内存场景直接用 `sessions`。

## 后果

- 会话恢复是"打开即恢复"：内存有就复用，没有就问 `session/load`；
- 分支 / 分叉（fork）没做；压缩的投影折叠只留了 `compact/checkpoint` 事件位；
- 工作区只做路径边界，不做文件系统沙箱（符号链接解析后越界会被拒，但工作区
  内指向外部的挂载点需要调用方自己注意——`resolve()` 已经跟随真实路径判断）。

## 相关

- [0042](0042-p4-base-boundary.md)（基座边界）、
  [0046](0046-prompt-plugins.md)（提示词组）；
- P1 决策 [0004](../0004-event-sourcing.md)（日志是唯一真相，消息由投影派生）。
