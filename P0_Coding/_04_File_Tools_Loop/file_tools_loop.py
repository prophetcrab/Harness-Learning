"""_04_File_Tools_Loop —— 带文件读写工具的循环：让模型能真正"动"你的磁盘。

前三个练习的工具都是"只读"的（计算、搜索）；本练习第一次给模型**写权限**，
因此引入两个新东西：
1. 工作目录约束（sandbox）：所有路径被限制在一个 demo 工作区里，越界直接拒绝
2. 人工审批：write_file 在真正写盘前暂停，等你在终端输入 y/n —— 
   这是 harness"有副作用的操作要人把关"的最小形态（M2 主题）

工具集（三个工具，覆盖最小 harness 的核心面）：
    read_file(path)             读文件（只读，自动审批放行）
    write_file(path, content)   写文件（有副作用，需要你确认）
    list_files()                列出工作区文件（只读）

用法：
    ../../.venv/Scripts/python.exe file_tools_loop.py
    ../../.venv/Scripts/python.exe file_tools_loop.py "把项目介绍改短一点"
    ../../.venv/Scripts/python.exe file_tools_loop.py --no-approve "..."   跳过审批（演示用）
    ../../.venv/Scripts/python.exe file_tools_loop.py --tool-only list_files   只测工具，不调模型
"""

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 工作区：模型只能在这个目录里读写，越界一律拒绝
WORKSPACE = Path(__file__).resolve().parent / "demo_workspace"
WORKSPACE.mkdir(exist_ok=True)

# 命令行参数
auto_approve = "--no-approve" in sys.argv
tool_only = "--tool-only" in sys.argv
positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
DEFAULT_QUESTION = "请读取 项目介绍.md 的内容，然后把它改写得更简洁（保持原意），写回同一个文件。"
question = positional[0] if positional else DEFAULT_QUESTION

# =========================================================================
# 1) 工具说明书 —— 三个工具一起给模型（这是最小 harness 的核心工具面）
# =========================================================================
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内一个文本文件的内容。路径相对于工作区，例如 '项目介绍.md'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对于工作区）"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入工作区内的一个文本文件（会覆盖原有内容）。写入前需要用户确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对于工作区）"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出工作区内的所有文件，用于了解有哪些文件可用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# =========================================================================
# 2) 工具实现
#    安全关键：所有路径都经 resolve_workspace_path() 检查，确保解析后
#    仍在 WORKSPACE 内 —— 模型给的 "../../../etc/passwd" 之类的路径必须被拒绝。
# =========================================================================
def resolve_workspace_path(path: str) -> Path:
    """把相对路径解析到工作区内；越界或非法路径直接抛异常。"""
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValueError(f"非法路径：{path}（只允许工作区内的相对路径）")
    resolved = (WORKSPACE / path).resolve()
    if not resolved.is_relative_to(WORKSPACE.resolve()):
        raise ValueError(f"路径越界：{path}")
    return resolved


def read_file(path: str) -> dict:
    """读取工作区内的文本文件。"""
    target = resolve_workspace_path(path)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    text = target.read_text(encoding="utf-8")
    return {"path": path, "content": text, "chars": len(text)}


def write_file(path: str, content: str) -> dict:
    """写入工作区内的文本文件。是否真正写入由审批钩子决定（见 needs_approval）。"""
    target = resolve_workspace_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "bytes_written": len(content.encode("utf-8")), "ok": True}


def list_files() -> dict:
    """列出工作区内的所有文件（相对路径 + 字节数）。"""
    files = []
    for item in sorted(WORKSPACE.rglob("*")):
        if item.is_file():
            files.append({"path": str(item.relative_to(WORKSPACE)).replace("\\", "/"),
                          "bytes": item.stat().st_size})
    return {"files": files, "count": len(files)}


# =========================================================================
# 3) 审批：哪些工具需要人工确认，以及怎么确认
#    这是 harness 权限模型的雏形：按工具分类（副作用强弱）决定是否拦。
#    只读工具直接放行；有副作用的工具暂停等待终端输入；无终端时一律拒绝
#    （fail-closed：不确定就不做）。
# =========================================================================
READ_ONLY_TOOLS = {"read_file", "list_files"}


def approve(name: str, arguments: dict) -> bool:
    """返回本次调用是否被允许执行。

    只读工具直接放行；有副作用的工具暂停等终端确认；
    无交互终端时一律拒绝（fail-closed：不确定就不做）。
    """
    if name in READ_ONLY_TOOLS:
        return True
    if auto_approve:
        print(f"  [审批] --no-approve：自动放行 {name}")
        return True

    print(f"\n  ⚠ 审批请求：模型想执行 {name}")
    print(f"    参数：{json.dumps(arguments, ensure_ascii=False)[:300]}")
    try:
        answer = input("    允许吗？(y/n): ").strip().lower()
    except EOFError:  # 没有交互终端时 fail-closed
        print("    （无交互终端，按拒绝处理）")
        return False
    if answer not in ("y", "yes"):
        print("    已拒绝。")
        return False
    print("    已批准。")
    return True


# =========================================================================
# 4) 工具注册表 + 执行（含审批关卡）
#    执行顺序：查表 → 审批 → 执行 → 异常转结构化错误
# =========================================================================
tool_functions = {
    "read_file": read_file,
    "write_file": write_file,
    "list_files": list_files,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """执行工具；先过审批，再把异常转成结构化错误回给模型。"""
    if name not in tool_functions:
        return {"error": f"未知工具：{name}"}

    if not approve(name, arguments):
        return {"error": f"用户拒绝执行 {name}。请尊重用户决定，不要重试该操作，可向用户说明并询问下一步。"}

    try:
        return tool_functions[name](**arguments)
    except Exception as exc:
        return {"error": f"工具执行失败：{exc}"}


# =========================================================================
# 5) --tool-only 分支：不调模型，离线验证工具与安全约束
# =========================================================================
if tool_only:
    print(f"工作区：{WORKSPACE}")

    if positional and positional[0] != "list_files":
        # 允许直接传路径给 read_file 测试
        print(json.dumps(execute_tool("read_file", {"path": positional[0]}), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(execute_tool("list_files", {}), ensure_ascii=False, indent=2))

    # 顺带演示越界被拒（不实际写盘）
    boundary = execute_tool("read_file", {"path": "../../../etc/passwd"})
    print("\n越界测试（读工作区外的文件）：")
    print(json.dumps(boundary, ensure_ascii=False, indent=2))
    raise SystemExit(0)

# =========================================================================
# 6) 读取项目根目录 .env 里的 API 配置（与前几个脚本相同）
# =========================================================================
env_file = PROJECT_ROOT / ".env"
if env_file.is_file():
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

if not api_key:
    raise SystemExit(
        "\n未找到 DEEPSEEK_API_KEY，无法调用模型。\n"
        "请在项目根目录 .env 中写入：DEEPSEEK_API_KEY=sk-...\n"
        "只想验证文件工具的话，加上 --tool-only list_files 运行（不需要 key）。\n"
    )

# =========================================================================
# 7) 主循环：模型 → 工具 → 模型 ……（与 _03 相同的形状，多了审批关卡）
# =========================================================================
print("文件工具循环演示")
print(f"工作区：{WORKSPACE}")
print(f"问题：{question}")
print(f"审批模式：{'自动放行（--no-approve）' if auto_approve else '写操作需人工确认'}")
print("—" * 60)

system_prompt = (
    "你是一名中文技术助手，可以读写工作区内的文件。"
    "需要文件内容时用 read_file，需要修改或创建文件时用 write_file，不了解环境时先 list_files。"
    "不要在回答里编造文件内容。最终回答用中文，简洁，说明你做了哪些操作。"
)

messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": question},
]

client = OpenAI(api_key=api_key, base_url=base_url)
max_rounds = 8

model_calls = 0
tool_executions = 0

for round_no in range(1, max_rounds + 1):
    model_calls += 1
    print(f"\n[模型第 {round_no} 次调用] 发出 {len(messages)} 条消息")

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        tools=tools_schema,
    )
    message = response.choices[0].message

    if not message.tool_calls:
        print(f"  → 返回：最终回答（finish_reason={response.choices[0].finish_reason!r}）")
        print("\n════════ 最终回答 ════════")
        print(message.content)
        break

    print(f"  → 返回：申请调用 {len(message.tool_calls)} 个工具")
    messages.append(
        {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {"id": call.id, "type": "function",
                 "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in message.tool_calls
            ],
        }
    )

    for call in message.tool_calls:
        tool_executions += 1
        name = call.function.name
        arguments = json.loads(call.function.arguments)
        print(f"[执行第 {tool_executions} 次] {name}({json.dumps(arguments, ensure_ascii=False)[:150]})")

        result = execute_tool(name, arguments)
        if "error" in result:
            print(f"                 → 失败：{result['error'][:120]}")
        elif name == "read_file":
            print(f"                 → 成功：读取 {result['chars']} 字")
        elif name == "write_file":
            print(f"                 → 成功：写入 {result['bytes_written']} 字节")
        else:
            print(f"                 → 成功：{result.get('count', 0)} 个文件")

        messages.append(
            {"role": "tool", "tool_call_id": call.id,
             "content": json.dumps(result, ensure_ascii=False)}
        )

    print("  → 结果已回填，继续下一轮")
else:
    print(f"\n达到最大轮数 {max_rounds} 仍未得出最终回答，已停止。")

# =========================================================================
# 8) 运行统计 + 提示
# =========================================================================
print("\n════════ 本次运行统计 ════════")
print(f"模型调用：{model_calls} 次   工具执行：{tool_executions} 次")
print(f"工作区当前文件：")
for item in sorted(WORKSPACE.rglob("*")):
    if item.is_file():
        print(f"  - {item.relative_to(WORKSPACE)}  ({item.stat().st_size} 字节)")
print("提示：write_file 的审批发生在 execute_tool 里；被拒绝时错误会回填给模型，")
print("      模型不会重试，而是向你说明情况 —— 这就是'错误也是给模型的输入'。")
