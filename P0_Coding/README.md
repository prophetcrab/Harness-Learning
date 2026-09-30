# P0_Coding

P0 阶段的编码工作区：用单文件脚本把 harness 的核心机制逐个跑通。

**状态：已完成（2026-09-30，4 个练习、15 个测试用例全绿）。**
后续的结构化练习（M1–M3）见 [P1_Coding](../P1_Coding/README.md)。

> 📖 **环境配置、每个脚本的运行方式、常见问题**，见项目根目录的 [README.md（使用说明书）](../README.md)。

## 约定（P0 期间，保留备查）

- 每个编码任务独立一个子目录（如 `P0_Coding/<task-name>/`）。
- 任务开始前先写验收标准（可运行的测试或命令），跑通即完成。
- 每个练习配 `run.bat` / `run.sh` 一键启动器（双击 `run.bat` 可运行）；`.bat` 内保持纯 ASCII。
- 与 `docs/learning-plan.md` 对应阶段的产出在此落地；阶段结束同步更新进度表。

## 当前内容

- `_01_LLM_Calling/` —— 最小化模型调用 + 提示词拼装（2026-09-30 完成）
  - 脚本：`llm_calling.py`（单文件平铺，直接运行）；验收：`pytest -q` 2 个用例全过；真实 API 调用已跑通
  - 凭证：项目根 `.env`（已加入 `.gitignore`）
- `_02_Tool_Calling/` —— 最小化工具调用 + 结果展示（2026-09-30 完成）
  - 脚本：`tool_calling.py`（Bing 网络搜索工具、无需搜索 API key）；验收：`pytest -q` 3 个用例全过；完整链路已验证
  - 观察脚本：`inspect_model_calls.py` —— 逐次打印每次模型调用的请求与原始返回（可见无状态与 token 增长）
  - 离线排查用 `--search-only`，不需要模型 key
  - 一键运行：`./run.bat "问题"` / `./run.bat inspect "问题"`（或双击 `run.bat` / `bash run.sh "问题"`）
- `_03_Calculator_Loop/` —— 模型 → 工具 → 模型的循环演示（2026-09-30 完成）
  - 脚本：`calculator_loop.py`（离线安全计算器：AST 白名单求值，拒绝 `eval` 式代码执行）
  - 亮点：循环日志可见"申请/执行分离"与消息增长；`--no-tools` 对照实验；`--calc-only` 离线测工具
  - 验收：`pytest -q` 5 个用例全过；完整循环与对照实验均已真实跑通
- `_04_File_Tools_Loop/` —— 带文件读写工具的循环（2026-09-30 完成）
  - 脚本：`file_tools_loop.py`（read_file / write_file / list_files + 工作区沙箱 + 写操作人工审批）
  - 亮点：路径越界防护（resolve + is_relative_to）；审批拒绝后模型不重试、如实说明
  - 验收：`pytest -q` 5 个用例全过；批准/拒绝两条路径均已真实跑通
