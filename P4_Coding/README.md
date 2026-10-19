# P4_Coding —— 实质化（🏗️ 基座开工）

P4 是**一个项目**（不再是 P2/P3 那样的阶段副本目录）：插件底座 + 三类插件
（基础 / 应用化 / 能力），逐步补成一个可用产品。

- **目标与验收**：[PLAN.md](PLAN.md)（M1 拆步表在 M1 一节）
- **定调决策**：[0041](../docs/decisions/0041-p4-substantiation.md)（为什么自研底座 / 三类插件的判据 / 为什么不再用阶段副本）
- **基座边界**：[0042](../docs/decisions/0042-p4-base-boundary.md)（已采纳：非插件只有 core 一处——框架 + 引导（自带启动器）；树 / 装配单 / 热插拔是 loader 插件）
- **重写方法**：[0043](../docs/decisions/0043-p4-rewrite-from-scratch.md)（全部从零写、P3 只作参考、逐个小步）
- **当前状态**：**M1 完成 + M2 完成**（500 用例）：基座八步 / 动态 inject / 插件级依赖 / LLM 能力组（含工具调用）/ 提示词组 / 会话与项目组 / 工具面 / **Agent 三件套**（`agent` + `agent-loop` + `agent-admin`，跑通一个真 turn）/ **YAML 装配单**（launcher + 数据，改产品形态不改代码）
- **素材来源**：P3 **不搬代码**（语义对照 + 教训清单），见 PLAN 第 5 节

## 目录（规划，随里程碑长出）

| 目录 | 角色 | 现状 |
|---|---|---|
| `core/` | **非插件只有这一处**：框架（服务 / 事件 / effect / 生命周期 / 变更锁）+ 引导（`launcher.py`：`boot(装配单)` / 产品政策 / `--dump`） | ✅ M1 完成（步 1–8） |
| `plugins/base/` | 基础插件（默认 role=base）：`loader`、`llm`、`prompt`、`sessions`、`workspace`、`project`、`fs`、`proc`、`tools_admin`（tool-admin）、`agent`（+ capability 组的 `agent/loop`、`agent/admin`） | ✅ |
| `plugins/capability/` | 能力插件，按功能域分二级目录：`llm/`（provider / retry / tokens_usages / fake）、`agent/`（loop / admin）、`prompt/`（character_admin / system_prompt_admin）、`session/`（store_jsonl）、`fs/`（sandbox / observation_policy）、`proc/`（backends）、`workspace/`（admin）、`tools/`（math / fs / shell / fs_search / web_search / ask_user） | ✅ |
| `plugins/application/` | 应用化插件（换掉它产品形态变了）：`web` / `cli` / `sdk` | M4 才长出 |
| `harness/` | **公开门面**（`pip install p4-harness` 后的 `import harness`）：纯 re-export，不加载插件 | ✅ |
| `tests/` | 机制验收 + **功能目标验收**（`test_feature_goals.py` / `test_long_chain_and_concurrency.py` 离线；`test_real_api.py` 真实模型） | 536 用例离线；另 34 条真实 API（默认跳过） |
| `manifests/` | 装配单（产品形态就是数据）：`minimal.py`（离线）/ `realtime.py`（真实模型）/ `minimal.yaml`（YAML 版，同一形状） | ✅ |
| `docs/` | 图解：[ctx 如何管理插件](docs/ctx-plugin-management.html)（15 步交互演示，直接用浏览器打开） | ✅ |

## 怎么跑

    cd P4_Coding
    python -m pytest tests -q      # 每小步的定向验收（离线，默认不联网）

    # 真实 API 功能验收（DeepSeek，联网 + 计费；默认跳过）
    python -m pytest tests/test_real_api.py --real -q

    # 同一批功能目标的离线版本（剧本模型，其余链路全真）
    python -m pytest tests/test_feature_goals.py -q

    # 长链路 / 并行会话 / 会话内编辑（离线，含并发压力）
    python -m pytest tests/test_long_chain_and_concurrency.py -q

    python app.py                                   # 交互式（离线剧本）
    python app.py --realtime --yes --ask "你好"      # 真实模型
    python app.py --manifest manifests/minimal.yaml --dump
    python app.py --manifest manifests/minimal.yaml --yes --ask "你好"

YAML 装配单只是**数据**：插件工厂仍来自 `manifests/minimal.py` 的
`catalog()` 白名单；`--manifest` 与 `--realtime` 互斥，因为 YAML 里两条
路由（`fake` / `deepseek`）都已装好，用 `--provider` 选即可。

## 作为库使用

启动器在 core 里，可以打包分发（决策
[0056](../docs/decisions/0056-packaging-and-public-surface.md)）：

    pip wheel . -w dist          # 构建
    pip install p4_harness-*.whl

    import harness

    entries = harness.load_manifest("app.yaml", variables={"workspace": "ws"})
    runtime = harness.boot(entries, catalog={"my-plugin": MyPlugin})
    try:
        runtime.ctx.require("my-service")
    finally:
        runtime.close()

`import harness` 不会加载任何插件；插件按鸭子类型书写
（`name` / `inject` / `optional` / `requires` / `setup`），不需要 import
这个包。`boot()` 只依赖随包分发的 `loader`，其余插件全由调用方的 `catalog`
决定。
