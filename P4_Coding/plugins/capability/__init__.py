"""能力插件（默认 role=capability）：装卸只多 / 少一件事，产品形态不变。

按**功能域**分二级目录：

    llm/        模型接入：provider（通用适配器）/ retry（重试）/ tokens_usages（统计）/ fake
    prompt/     提示词：character_admin（角色模板）/ system_prompt_admin（检查与编辑）
    session/    会话：store_jsonl（JSONL 持久化）
    fs/         文件策略：sandbox（四种审批模式）/ observation_policy（改前必读）
    proc/       命令执行后端：backends（Git Bash / POSIX / 剧本）
    workspace/  工作区管理：admin（登记 / 切换 / 查看）
    tools/      具体工具：math / fs / shell / fs_search / web_search / ask_user

**目录只表达分组**：插件的身份仍是各自的 `name`（如 `tool-fs`、`fs-sandbox`），
装配单按 `name` 引用，与目录无关。文件名的前缀（`tool_`）由目录承担，所以
`tools/fs.py` 里的插件名仍是 `tool-fs`——两者不要混。
"""
