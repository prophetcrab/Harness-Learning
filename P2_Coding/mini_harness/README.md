# mini_harness —— P2 整合包（全部功能的单一可运行目录）

这是 **P2（M4–M7）全部功能的整合产物**：从 `_01`–`_11` 十一个阶段的最终状态
（= `_11_Session_Follow` 完成后的代码）收敛成一个自包含目录——`cd` 进来就能用
完整能力，不必再挑阶段目录。

> **本目录是快照，不是开发位置。** 后续修改在阶段目录（或新阶段）里进行；
> 整合包在里程碑处重新生成。阶段目录是"开发历史 + 教学注解"，本目录是"成品"。

## 包含什么（功能 ← 来源）

| 机制 | 来源 | 位置 |
|---|---|---|
| Agent 主循环 / 工具管线 / 会话日志（基线） | P1 `_05` | `harness/`（冻结基线，未改） |
| 提示词 section 注册表 + 作用域遮蔽 | `_01` | `prompt/registry.py`、`prompt/section.py` |
| 变量插值 + 每 step 渲染 + `system/message` 进日志 | `_02` | `prompt/interpolate.py`、`context/` |
| 装配单（来源追溯）+ 重建断言 | `_03` | `prompt/trace.py`、`config/dump.py` |
| FileSystem 接缝（Local / Memory） | `_04` | `providers/local.py`、`providers/memory.py` |
| 工作区围栏（策略型 provider） | `_05` | `providers/jail.py` |
| Subprocess 接缝 + `shell` 工具 | `_06` | `providers/subprocess*.py` |
| 插件协议 + effect 回卷 | `_07` | `kernel/` |
| profiles 分层 patch（一行换 provider） | `_08` | `config/profiles.py`、`profiles/*.yaml` |
| dump-config 来源台账 + 配置错误定位 | `_09` | `config/dump.py`、`config/patch.py` |
| stdio/TCP JSON-RPC（initialize / prompt） | `_10` | `server/`、`serve.py` |
| 事件流跟随（重放+订阅）+ attach 断线补齐 | `_11` | `server/service.py`、`attach.py` |
| 编码工具（edit / search / find） | `_11` | `providers/toolbox.py` |
| 可视化页面（P2 形态：全工具 + 插件台账） | `_11` | `webui/` |
| 可视化页面（M1–M3 基线形态，保留对照） | P1 `_05` | `harness/webui/` |

工具面共 8 件：`calculate` · `read_file` · `write_file` · `list_files` ·
`edit_file` · `search_text` · `find_files` · `shell`（外加可选 `web_search`）。

## 快速开始

```bash
cd P2_Coding/mini_harness
python -m pytest -q                     # 66 个用例（22 基线 + 44 机制），全离线

# 对话（配置驱动：dev 离线 / prod 真实 API）
python chat.py --profile dev --ask "帮我算 2+3"
python chat.py --profile prod --ask "用 shell 看看当前目录"   # 读项目根 .env 的 key
python chat.py --dump-config             # 配置树 + 每项来源层

# 服务化
python serve.py                          # stdio JSON-RPC
python serve.py --listen 8765            # TCP：多客户端共享同一会话
python attach.py --session s1            # 跟随（重放+实时，断线补齐）
python attach.py --connect 8765 --session s1

# 可视化
python webui/server.py                   # P2 形态页面（:8766）
python -m harness.webui.server           # M1–M3 基线页面（:8765，对照用）
```

一键启动器：`run.bat` / `run.sh`（`chat` / `serve` / `attach` / `web` 路由齐备，
无参数即离线 demo）。

## 目录

```
mini_harness/
├── harness/          ← M1–M3 基线库（冻结；含基线可视化 harness/webui/）
├── prompt/ context/  ← M4：提示词装配（section / 插值 / 装配单）
├── providers/        ← M5：能力接缝（fs / subprocess / 编码工具）
├── kernel/           ← M6：插件协议 + effect
├── config/ profiles/ ← M6：配置分层 / dump / 来源台账
├── server/ webui/    ← M7：RPC 传输 / 跟随 / P2 形态页面
├── chat.py demo.py serve.py attach.py   ← 入口
├── tests/            ← 全部验收用例（文件名加 mini_harness_ 前缀防重名）
└── run.bat run.sh conftest.py
```

## 与阶段目录的关系

- **阶段目录（`_01`–`_11`）**：开发历史。每个阶段自包含、只加一个机制，
  目录间不共享代码（P2 约定）；它们的名称、README 与注释保留当时语境。
- **本目录**：成品快照。全部机制叠在一起、入口齐备；测试是本阶段套件的副本
  （重命名以免与阶段目录的测试在"全量跑"时冲突）。

## 测试说明

`python -m pytest -q` 共 66 用例：22 个 M1–M3 基线（组装 / 工具 / 日志 / 基线页面）
+ 44 个 P2 机制（提示词装配与追溯、接缝、插件 effect、配置分层与定位、RPC、
跟随与断线补齐、编码工具、P2 页面）。全离线（FakeLLM + MemoryFS + 剧本命令），
不联网、不消耗模型额度。
