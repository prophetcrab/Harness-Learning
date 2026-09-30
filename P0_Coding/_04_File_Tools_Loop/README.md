# _04_File_Tools_Loop —— 带文件读写工具的循环（第一次给模型"写权限"）

P0 第四个练习：在循环里加入 `read_file` / `write_file` / `list_files` 三个文件工具，
凑齐最小 harness 的核心工具面（**文件 + 计算 + 网络**，其中计算和网络在前两个练习里已有）。
这是第一次让模型能真正**修改你的磁盘**，因此引入两个新机制：

1. **工作区沙箱**：所有路径限制在 `demo_workspace/` 内，越界（`..`、绝对路径、软链逃逸）直接拒绝；
2. **人工审批**：写操作在落盘前暂停等你输入 y/n；无交互终端时一律拒绝（fail-closed）。

## 学习目标

1. 理解 **工具按副作用分级**：只读工具（read/list）放行，写工具（write）需审批——这是 harness 权限模型的雏形。
2. 掌握**路径防护**的正确做法：不是字符串比较 `startswith`，而是解析真实路径后判断包含关系（`Path.resolve()` + `is_relative_to`），能挡住 `../../` 和符号链接逃逸。
3. 观察一个行为细节：**审批被拒绝时，模型不重试**——错误回填后它会向你说明情况并给替代方案。

## 文件

| 文件 | 说明 |
|---|---|
| `file_tools_loop.py` | 主脚本：三个工具定义 + 实现 + 沙箱 + 审批 + 主循环 |
| `demo_workspace/项目介绍.md` | 演示工作区（模型改写这个文件） |
| `run.bat` / `run.sh` | 一键运行启动器 |
| `test_file_tools_loop.py` | 验收测试（5 个离线用例） |

## 运行

```bash
cd P0_Coding/_04_File_Tools_Loop

# 一键运行（双击 run.bat 等价；写文件时会问你 y/n）
./run.bat                                              # 默认问题：改写 项目介绍.md
./run.bat "在工作区新建一个 README.md，写三行介绍"
./run.bat --no-approve "在工作区新建一个 demo.txt"       # 自动放行（演示用）
./run.bat --tool-only list_files                        # 只测工具，不需要 key

# 手动运行（等价）
../../.venv/Scripts/python.exe file_tools_loop.py
../../.venv/Scripts/python.exe file_tools_loop.py --tool-only "项目介绍.md"

# 验收测试
../../.venv/Scripts/python.exe -m pytest -q
```

## 一次真实运行（拒绝写入的场景）

```
[模型第 1 次调用] 发出 2 条消息
  → 返回：申请调用 1 个工具
[执行第 1 次] list_files({})
                 → 成功：1 个文件
[模型第 2 次调用] 发出 5 条消息
  → 返回：申请调用 1 个工具
[执行第 2 次] read_file({"path": "项目介绍.md"})
                 → 成功：读取 251 字
[模型第 3 次调用] 发出 7 条消息
  → 返回：申请调用 1 个工具
[执行第 3 次] write_file({"path": "项目介绍.md", "content": "..."})

  ⚠ 审批请求：模型想执行 write_file
    允许吗？(y/n): n
    已拒绝。
                 → 失败：用户拒绝执行 write_file。请尊重用户决定，不要重试该操作...

════════ 最终回答 ════════
已读取 `项目介绍.md`（原约 251 字）。我改写了一版更简洁的文本，但在写入时你拒绝了该操作，
因此原文件未被修改（内容保持原样）。改写稿如下，供你参考：...
```

模型**没有重试写入**，而是说明情况+给出改写稿+询问下一步。文件保持原样。

## 验收标准

- [x] `--tool-only list_files` 正确列出工作区文件
- [x] `--tool-only "项目介绍.md"` 正确读出内容（含字数）
- [x] 读不存在的文件 → 结构化错误（不崩溃）
- [x] 越界路径 `../../../etc/passwd` → 被拒绝：`非法路径`
- [x] 无 key 时明确报错并提示 `--tool-only` 出路
- [x] 完整循环实测：批准路径（文件 599→414 字节）与拒绝路径（文件不变）都符合预期
- [x] `pytest -q` 全部通过（5 个用例）

## 三个安全设计（值得记住）

```
1. 路径防护：resolve() 后判断 is_relative_to(WORKSPACE)
   —— 字符串 startswith 检查挡不住 ../ 和符号链接

2. 审批分级：READ_ONLY_TOOLS 集合决定哪些免审
   —— harness 里更完整的做法是"策略 + 一次一议"，见 dsh 的 ctx.approval

3. fail-closed：拿不到用户输入时默认拒绝
   —— 不确定就不执行，宁可少做不可做错
```

## 与 harness 概念的对应

| 本练习 | dsh 对应 |
|---|---|
| `WORKSPACE` 约束 | `ctx.sandboxPolicy`（工作区根）+ `fs-sandbox` |
| `approve()` 询问 y/n | `ctx.approval` 一次性审批 + answerer 瀑布 |
| `READ_ONLY_TOOLS` 分级 | 工具的 `needsApproval`/审批策略 |
| 路径防护 | `FileSystem.processPath` / `contains` 校验 |
| 拒绝后的错误回填 | 铁律 #7"错误是给模型的输入" |

## 已知限制（故意的，后续练习解决）

- 工作区刷新依赖手动；没有"读前写"策略（dsh 的 `fs-observation-policy` 要求先读后写，防止盲改）；
- 审批是简单的按工具名分级，没有会话级策略（`/permission` 那种）；
- 一次运行一个会话，退出即忘（M3 会话日志解决）。
