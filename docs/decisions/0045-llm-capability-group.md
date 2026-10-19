# 0045 LLM 能力组：主插件 + 三个子插件

- 日期：2026-10-16
- 阶段：P4_Coding（M2）
- 状态：已采纳

## 背景

用户给定 LLM 能力组的结构（原话）：

> llm 能力插件：定位 core 不可卸载；需要支持会话选择和修改 LLM API 的调用配置，
> 同时需要满足子代理可以选择不同模型的需求。
> llm-provider：通用适配器，把方言翻译成标准请求，提供可定制化的参数；
> llm-retry：重试策略；
> llm-tokens-usages：消耗统计：总消耗 tokens、日消耗 tokens、本对话消耗 tokens、
> 总缓存命中率、日缓存命中率、本对话缓存命中率（日缓存命中率应该为应用用户级别的
> 独立管理）。
> 按照这个来实现 provider 由三个子插件组成，provider 不需要依赖子插件完整，
> 但是应该返回已依赖但缺乏的子插件。

## 决策

### 一、结构：一个主插件 + 三个独立子插件

| 插件 | 角色 | 职责 | 缺了会怎样 |
|---|---|---|---|
| `llm` | base（不可卸载） | 路由表、请求校验、模型信息解析、**子插件状态报告** | — |
| `llm-provider:<route>` | capability | 方言翻译（标准请求 ↔ 线格式）、连接、可定制参数 | 没有路由可调用 |
| `llm-retry` | capability | 订阅 `llm/call` waterfall，包住调用 | 出错不重试 |
| `llm-tokens-usages` | capability | 订阅 `llm/finish`，三维度记账 | 不统计 |

**主插件不依赖子插件的服务**（`llm` 的 `inject` 为空）：缺 provider 时"没有可用路由"
是运行时错误（`NO_PROVIDER`）；缺 retry / 统计时行为照常，只是少一项。

**但 provider 是插件级依赖**（2026-10-16 补充）：`LLMPlugin.requires = ("llm-provider",)`。
没有适配器时路由层虽然能起，任何调用都会失败——属于**装配不完整**，由 loader
在装配 / 热插拔后 fail loud，而不是等到第一次请求。

为什么用 `requires` 而不是 `inject`：

    provider 插件反过来依赖 llm 服务（要 register）
    若 llm 也 inject provider，两边互相等 → 谁都不会激活（死锁）
    requires 不驱动生命周期，只做装配期校验——正好表达"要有一个，但别等它"

`llm-fake` **不算**满足 `llm-provider`：假适配器是测试替身，不是"产品有了一条
模型路由"。这条由测试钉住（`test_llm_assembly_needs_a_real_provider`）。

**报告缺失**：`status()` / `missing_sub_plugins()` / `sub_plugins()`。判断依据是
**插件台账里有没有这个名字**（支持 `llm-provider:<route>` 多实例），不是服务槽。

### 二、为什么拆成三个子插件，而不是一个

1. **可独立装卸**（0041 的判据：装卸只多/少一件事）：重试策略、用量统计都不该
   成为"LLM 能用"的前提；离线测试可以只装 `llm` + 假适配器。
2. **策略插座有真实使用者**：`llm-retry` 挂在 `llm/call` waterfall 上——这正是
   0042 要闭的第三处差距（"策略层有插座"）。它证明了 waterfall 不是摆设。
3. **两组事件**：`llm/call`（waterfall，可包装调用）与 `llm/finish`（emit，
   只读通知）。统计只订阅后者，绝不侵入调用路径。

### 三、统计的分桶口径

| 视图 | 分桶键 | 说明 |
|---|---|---|
| `totals` | 无 | 进程内累计（落盘留给 M5） |
| `today_total` | 无 | **今日合计**（所有用户加起来）——"日消耗 tokens" |
| `today_by_user[user]` | `request.user_id`（缺省 `anonymous`） | **日维度按应用用户独立管理**（用户的要求） |
| `sessions[session_id]` | `request.session_id` | "本对话"；没有 session_id 的调用不计入 |

三项数字各自带 `calls / input / output / total / cached / cache_hit_rate`。
跨天自动清零"今日"的两个桶（总量与会话桶保留）。没有 usage 的调用也计入
`calls`（"有调用却零 token"要能被看见）。

**只读对外**：服务 `llm-tokens-usages` 提供的是 `TokenUsageView`——只有
`snapshot()` 和 `totals` / `today` / `sessions` 三个便利属性，**没有 `record()`**。
记账只发生在插件自己的 `llm/finish` 处理器里；别的插件拿到服务也改不了数字。

### 四、可定制参数的合并顺序

内置字段 → `default_params`（装配单里的插件配置）→ `request.extra`（某一次调用）。
连接类配置（`base_url` / 密钥 / 超时）属于**插件 config**：改它 = `loader.update`
换一个新适配器实例（热插拔，M1 已具备）。密钥只从环境变量取（`api_key_env`），
不进装配单。

**多模态内容同样走这条路**：`Message.content` 可以是字符串或内容块序列
（`TextPart` / `ImagePart`），方言层负责编码成各家的线格式。图片能否发送由
`ModelInfo.supports_images` 决定，路由层在调用前校验（0044 第四节）。

### 五、重试的边界

可重试：连接 / 超时 / 限流 / 5xx。
**不可重试**：凭据、请求非法、无适配器、无模型——重试只会再失败一次。

**流式安全**（2026-10-16 修）：重试只在"还没吐出任何块"的时候发生。首块一旦
交给调用方就不再重试——否则已输出的内容会被再吐一遍（重复文本）。首块是
错误 `Finish` 时仍可重试（此时还没输出任何东西）。用尽重试后抛出**最后一次的
原始异常**（保留 `code`，消费方按它分支）。

配套地，`llm.stream()` 本身也**逐块透传**（不在主干 collect）——否则"装了重试
就退化成非流式"的问题只是换了个地方发生。

## 备选与排除理由

**A. 一个插件全包（路由 + 重试 + 统计）**：任一项出问题都要动整个 LLM 能力；
离线测试也被迫带上重试与统计。否决。
**B. 子插件作为必选依赖（`inject`）**：用户明确要求"不要求完整"，且"缺 provider"
在离线 / 渐进装配里是合法状态。否决。
**C. 统计写进 llm 主干**：违反 0041 的判据（计量是能力，不是接入），且会让
主干依赖存储。否决。

## 后果

- `llm` 的多项能力可单独验收（三个子插件 + 完善项共 23 个用例）；
- 统计目前是**进程内**的：重启归零。跨重启持久化 + 管理面展示留给 M5/M3；
- 首块之后的失败不重试（这是流式安全的代价）：半截回答交给调用方处理，
  是否"整段重来"是上层策略。

## 相关

- [0044](0044-llm-router-and-model-selection.md)（模型选择是数据）、
  [0042](0042-p4-base-boundary.md)（策略插座）、[0041](0041-p4-substantiation.md)
  （三类插件）；
- dsh 对照：`dsh-llm`（主干）/ `dsh-llm-deepseek`（适配器）/
  `dsh-llm-retry`（重试）/ `dsh-token-meter`（计量）——同样是四个包，我们按
  用户要求把计量做成"总量 / 日 / 会话 + 缓存命中率"的更完整口径。

## 补充：物理目录按默认角色分（2026-10-16）

插件源码搬进按角色命名的目录，**物理位置跟着角色走，而不是跟着"组"走**：

    plugins/base/                loader.py、llm.py
    plugins/capability/llm/      provider.py、retry.py、tokens_usages.py、fake.py
    plugins/application/         （M4：web / cli / sdk）

要点：

- **目录是默认角色，不是最终裁决**（0035）：装配单里的 `role` 仍然可以覆盖；
- **LLM 这一组跨两个目录**：主插件在 `base/`（它是能力入口），子插件在
  `capability/llm/`（各自可装卸）——分组靠子目录，角色靠父目录；
- 由 `tests/test_purity.py::test_plugins_live_in_role_directories` 钉住：
  顶层 `plugins/*.py` 只允许包门面，出现插件模块即红（反向验证过）。
