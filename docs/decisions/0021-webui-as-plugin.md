# 0021 界面即插件：webui 做成插件（装载即起、卸载即停），离线模型分两档

- 日期：2026-10-13
- 阶段：P3_Coding（基础包增补）
- 状态：已采纳

## 背景

P3 基础包（决策 0020）交付后，七个标准插件都是"纯逻辑能力"（模型 / 文件 /
命令 / 工具面 / 会话 / 提示词 / agent）。被问到"界面不能也是插件吗"，答案是
能、且应该——但 P2 时代的 `webui/` 是手工起的服务：`create_server()` 建
`ThreadingHTTPServer`，主线程 `serve_forever()` 阻塞，`daemon_threads=True`，
**没有卸载路径**（进程退出是唯一清理方式）。端口、线程一旦起来就不可回收。

同时暴露出一个交互层面的真缺陷：离线默认用的是 `FakeLLM`（严格剧本队列），
**一问就把剧本用完，第二问直接报错**——测试里这是武器，界面里是折磨。

## 决策

**1) 新增 `plugins/webui.py`（第八个标准插件）+ `webui/index.html` 静态页**。
界面插件声明 `inject = ("agent", "sessions")`；`setup` 建服务/起线程/
`provide("webui", WebUiService)`，**返回 disposer**（`shutdown` + `join` +
`server_close`）。于是界面获得与其它插件完全一致的生命周期语义：

    ctx.load_plugin(webui_plugin(port=0))   # 装载即起（port=0 → 系统分配空闲端口）
    ctx.unload_plugin("webui")              # 卸载即停（端口真的被释放，demo 第 6 节验证）

**2) 三条并发/线程纪律**（写进代码注释与文档）：

- **生命周期只在主线程做**：内核是同步无锁的，装载/卸载不进服务线程；
- **服务线程只调服务、不改 ctx 状态**：HTTP handler 里只读 `ctx.require(...)`
  拿到的服务对象；
- **跑 turn 用一把锁串行化**：`agent` 的会话缓存（`_open` dict）与剧本 provider
  都不是线程安全的，`ThreadingHTTPServer` 每请求一线程，两个标签页同时提问
  会互相踩——插件用 `threading.Lock` 把 turn 串起来。

**3) 离线模型分两档，各归其位**：

    fake_llm_plugin(script)   FakeLLM        严格剧本，用完即报错（测试的武器）
    offline_llm_plugin(reply) RepeatingLLM   固定回复，不限轮数（界面/长演示）

`boot_offline(script=None)`（默认）走 RepeatingLLM，`script=[...]` 走 FakeLLM。
**测试纪律不因交互需要而放松**，交互也不因测试纪律而遭罪——两档并存，
由装配方选择（`test_p3_offline_llm.py` 把这条界线钉住）。

**4) 前端刻意"薄弱"**：`webui/index.html` 原生 JS、零依赖、无构建步骤。
理由与本项目其它部分一致——它是"看得到内核行为"的窗口（插件台账 / 会话 /
实时事件流），不是产品级前端；产品化前端属于 P5/P6 的题目。

## 备选与排除理由

1. **把页面做成独立进程/独立入口（照抄 P2 的 `webui/server.py`）**。排除：
   那正是要修的形态（无卸载路径、装配与主进程重复一份）。做成插件后
   "起/停/换"自动获得，`chat.py --web` 与 `/web` 只是薄入口。
2. **前端用框架（React/Vue）+ 构建链**。排除：会引入 node 工具链与
   `node_modules`，与"零依赖、cd 进来就能跑"的项目约定冲突；页面需求
   （三栏 + 流式拼帧）用 200 行原生 JS 足够。
3. **HTTP 服务用 `socketserver`/asyncio 自写**。排除：`http.server` 是标准库
   里最省心的选择；真正需要替换的（并发模型、鉴权、多客户端广播）属于
   RPC/事件流跟随的题目，届时替换的是"服务端插件"本身，生产者（agent）不动。
4. **让界面插件直接依赖 `fs`/`llm` 等底层服务**（少一层间接）。排除：那会把
   界面与底层实现绑死——换 fs 会连带重启界面（端口被动更换）。依赖链停在
   `agent` 这一层，界面只面对"会话与提问"的稳定语义。

## 后果

- **收益**：界面从"手工起的服务"变成"一对干净语义"（装载/卸载），且与
  插件体系完全同构——台账里 `webui ← agent,sessions` 一眼可见；`chat.py --web`
  与 `./run.bat web` 是同一个插件的两个入口。浏览器实测通过（页面渲染、
  新会话、发消息、事件流实时生长、状态栏保留）。
- **约束与已知边界**：
  1. **只绑本机、无鉴权**——故意：它是本地观察窗口；
  2. **事件流只推给发起 turn 的那个客户端**（多客户端广播未做）；
  3. **级联重启**：换底层插件（如 `fs`）会让依赖链上的 `webui` 一并退位/
     重启（端口随之更换）。这是"严格依赖 + 依赖者先退位"的直接后果，
     已写成显式用例（`test_webui_restarts_when_upstream_plugin_swapped`）；
     需要"换底层而不中断界面"时应走稳定门面（RPC 服务）。
- **债务与后续**：
  1. `webui` 与未来的 `rpc` 服务插件应共享"会话/事件"的读取路径，避免两套
     只读组装逻辑（当前 `_ledger` / `_sessions_overview` / `_session_detail`
     是页面专用的纯函数，可提取为服务）；
  2. 多客户端广播 + 断线补齐（P2 `_11` 的 `session.follow`）在 P3 中仍缺，
     属于"服务化插件"的题目；
  3. 页面无鉴权、无 HTTPS——本地窗口的定位使然，产品化时随平台档一起解决。
