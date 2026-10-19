# 0046 提示词：base 装配器 + 两个能力插件

- 日期：2026-10-16
- 阶段：P4_Coding（M2）
- 状态：已采纳

## 背景

用户给定的结构：

> prompt —— base，必要依赖：无
> character-admin：预设角色模板插件
> system-prompt-admin：系统提示词检查和编辑插件

## 决策

### 一、`prompt` 是 base，且**没有任何依赖**

它只做三件事：注册 section、注册变量、渲染。它不认识角色、工具说明书、
时间上下文——那些由各自的能力插件往它上面注册。所以：

- `inject` 为空：提示词是 harness 最底层的输入，不该等服务就绪；
- 渲染**不缓存**：每次 `render(**context)` 现算，调用方（agent-loop）按 step 调；
- 用 `RenderedPrompt.fingerprint` 判断"变没变"——变了才往会话日志追加一条
  系统提示词（P2 决策 0007 的语义），否则日志会被每 step 重复的文本灌满。

失败一律 fail loud：变量缺失、section 文本不是字符串、重名注册。静默留空会
让提示词悄悄少一段，那是最难查的错。

### 二、section 的顺序与开关

- `order` 小的在前；同 order 按注册顺序；
- `set_enabled(name, enabled)` 可以关掉一个 section（**不注销**：内容还在，
  只是不参与渲染）——这是 `system-prompt-admin` 的编辑落点；
- 关掉的 section 的文本函数**不会被调用**（不是渲染完再丢），所以它可以安全
  地访问只在特定上下文才存在的值。

### 三、character-admin：角色是"按会话"的选择

角色文本就是一段提示词。选择粒度与会话对齐（与"不同会话选不同模型"同一层）：

    characters.set("s1", "coder")
    characters.set("s2", "teacher")
    characters.active_name("s1")   # "coder"

- 内置 default / coder / reviewer / teacher 四个预设；
- 装配单可以追加新角色、**整段覆盖**内置角色（不符合口味就换掉）；
- `default` 配置决定没选过的会话用哪个；没有默认 → 不贡献任何提示词；
- 角色 section 的 order=10（人设先于具体说明）。

角色只影响提示词，不改模型、不改工具面。

### 四、system-prompt-admin：检查与编辑是同一份数据

    inspect(**context)          完整渲染结果 + section 表 + 变量 + 指纹
    override(session_id, text)  给某个会话追加一段说明（order=900，靠后）
    disable(name) / enable(name) 开关任意 section
    disabled_sections()         本插件关掉了哪些

**编辑在运行时、不回写装配单**（0042 的立场）：重启回到装配单声明的内容。

**卸载会恢复**：本插件关掉的 section 在卸载时重新打开，override section 随
effect 回卷。这条由测试钉住——管理面不能留下"某个 section 被永久关掉"的垃圾
状态。

**检查不另存一份**：`inspect()` 就是调用一次真实的 `prompt.render()`，因此
它看到的永远是当前真相，不会与页面/调用方拿到的不一致。

## 备选与排除理由

**A. 角色做成一等服务、提示词里硬编码角色名**：角色切换要改提示词代码。
否决。
**B. 检查器自己缓存一份提示词文本**：会与真实渲染漂移（P3 的教训：快照必须
实时读）。否决。
**C. 编辑直接改装配单**：那就有了"人手改的文件"和"机器改的状态"两个真相。
退回 0042 的立场：运行时变更不回写。否决。

## 后果

- 角色切换是**内存态**：重启回到装配单默认。要"记住用户选的角色"需要
  user overlay（M5 再定）；
- `override` 按 session_id 存在内存里，同理不持久；
- 提示词的所有贡献者都是插件：角色、override、将来的工具说明书、时间上下文
  各占一个 section，谁卸载谁的那段就消失。

## 相关

- [0042](0042-p4-base-boundary.md)（基座边界与"不回写装配单"）、
  [0041](0041-p4-substantiation.md)（三类插件）；
- P2 决策 [0007](../0007-prompt-context.md)（每 step 渲染、变化才记录）与
  [0008](../0008-prompt-trace.md)（装配单）。
