# 0058 P4 内核 async-first：从同步 + 线程锁改为单事件循环

- 日期：2026-10-29
- 阶段：P4_Coding（core 已改为 async-first；P0–P3 不变）
- 状态：已采纳

## 背景

P4 的 core 原本是同步内核：所有公共操作同步执行，`threading.RLock` 串行化变更，
插件 setup / effect / 事件 handler / inject callback 都要求同步函数。

但 cordis（dsh vendored 4.0.5）的核心是异步的：插件 fiber 的启动、effect 的
disposer、事件派发都能返回 Promise；异步不是附加能力，而是生命周期的形状。
在把 P4 core 与 cordis 逐项对照后，用户决定：**P4 的 core 做 async-first 重写**，
接受 API 断代，换取与 cordis 同形的异步生命周期。

之前 P3 的决策 [0020](0020-p3-plugin-core.md) 排除 asyncio，理由是 P1–P3 的循环、
管线、日志、RPC 全同步，"为更像 cordis 引入异步会污染整条链路"。P4 只是 core，
不存在这条约束；P1–P3 仍保持同步，不受本次改动影响。

## 决策

### 1. 公共生命周期 API 全部 async

    await ctx.provide / retract / effect / unload_effect
    await ctx.inject(...)            # 返回异步取消句柄：await handle()
    await ctx.load_plugin(...)       # 返回异步卸载句柄：await handle()
    await ctx.unload_plugin / disable_plugin / enable_plugin
    await ctx.emit(...) / await ctx.waterfall(...)
    await boot(...) / await runtime.close()

只读面保持同步：`revision`、`last_change`、`plugins`、`active_plugins`、
`pending_plugins`、`disabled_plugins`、`services`、`effects`、`injections`、
`event_names`、`get` / `require` / `missing_dependencies`。
`ctx.on(event, handler)` 保持同步（它只登记订阅、不推动稳定态），返回同步退订
函数。

### 2. 回调可以是同步或异步

插件 `setup`、effect 的 `install`、disposer、事件 handler、inject callback 都
允许同步或异步函数；core 统一 `await` 返回值里的 awaitable。这保留了简单回调
的写法，也让异步资源清理（async disposer）真正被等待。

### 3. 单事件循环 + 按 task 可重入的异步锁

- Context 不再线程安全：一个 Context 只能在一个事件循环里用。
- `threading.RLock` 换成 `_AsyncRLock`：同一 task 可重入（插件 setup 里再
  `await ctx.load_plugin` 合法），其他 task 排队；嵌套公共操作并入最外层变更。
- 锁在 `await` 插件代码期间仍持有：这保留了"变更串行化/稳定态"的语义，但也
  意味着**不要在持有锁的回调里 spawn 新 task 再等它操作同一个 Context**（会
  死锁）。这条约束写进 Context 文档。

### 4. 事件派发是顺序 await

`emit` / `waterfall` 按注册序 `await` 每个 handler；返回非 None 的
waterfall handler 照旧替换值。暂不做 cordis 的 `parallel` / `serial` / `bail`
变体——异步派发要等真实消费者出现再补。

### 5. loader 契约

`loader.assemble(manifest)` 可以是同步或异步函数（`boot` 会等待）；
`snapshot()` 保持同步（产品政策检查与 `dump` 都是同步读）。
loader 插件本身仍不在 P4 工作区（历史实现见 git）。

### 6. 同步 setup 的边界

允许同步 `setup`，但同步函数里无法 `await` 生命周期 API，调用它们只会得到
未 await 的协程（只有 RuntimeWarning）。因此约定：**要 provide / effect /
inject / 装卸插件的插件必须用 `async def setup`**；同步 setup 仅用于纯计算或
同步登记（如 `ctx.on`）。

## 备选与排除

1. **只加 fail-loud 守卫（原第 1 层）**：把 async 返回值直接报错，成本最低；
   但用户明确要 async-first，排除。
2. **双轨 API（同步核心 + 异步变体）**：会长期两套生命周期语义并存，违背
   "change 串行化/稳定态"只有一条实现路径的初衷，排除。
3. **保留 threading.RLock**：锁的持有者是线程，不是 task；在 `await` 期间同一
   线程的其他 task 会重入锁，等于没有串行化，排除。
4. **完全照 cordis 的 fiber/epoch 模型**：需要重写依赖追踪与热更新机制，
   超出本次"异步生命周期"的范围，留作后续。

## 后果

- **API 断代**：P4 的 core 测试全部迁移到 `pytest-asyncio`（`asyncio_mode=auto`）；
  所有生命周期调用都要 `await`，句柄调用也变成 `await handle()`。
- **收益**：async 插件的 setup/清理、异步事件 handler、异步 inject callback
  真正被等待；以前"async disposer 静默不执行 / waterfall 把 coroutine 当值"
  的坑消失。
- **新约束**：单事件循环、非线程安全、锁内 spawn task 会死锁；跨事件循环复用
  Context 会报错（asyncio 原语绑定 loop）。
- **未做**：事件并行派发、超时/取消、async 版本的 loader 热更新、多事件循环
  支持；这些等真实消费者。
- **不受影响**：P0–P3 仍为同步实现，各自门禁不变。
