"""_03_Calculator_Loop —— 模型 → 工具调用 → 模型 的循环演示（计算器工具）。

和前两个脚本的关系：
- _01 最小模型调用（无工具）
- _02 工具调用 + 网络搜索（Bing，联网）
- _03 本脚本：换一个离线计算器工具，把"模型 ⇄ 工具"的循环本身演示清楚

循环的形状（每一轮 = 一次模型调用）：
    组装 messages → 调模型
      ├─ 返回 tool_calls（申请）→ 本地执行工具 → 结果回填 messages → 下一轮
      └─ 没有 tool_calls（最终回答）→ 结束

运行日志里演示三个要点：
1. "申请"和"执行"分离：模型只说"请调 calculate(参数)"，真正运行的是本文件的 Python 代码
2. 模型无状态：每轮都打印"发出 N 条消息"，可以看到历史逐轮变长
3. 安全性：计算器用 AST 白名单求值，绝不 eval() 模型生成的字符串（那等于交出代码执行权）

用法：
    ../../.venv/Scripts/python.exe calculator_loop.py
    ../../.venv/Scripts/python.exe calculator_loop.py "帮我算 ..."
    ../../.venv/Scripts/python.exe calculator_loop.py --no-tools "帮我算 ..."   对照实验：不给工具，模型只能心算
    ../../.venv/Scripts/python.exe calculator_loop.py --calc-only "2+3*4"     只测计算器本身（不需要 key）
"""

import ast
import json
import operator
import os
import sys
from pathlib import Path

from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 命令行参数
use_tools = "--no-tools" not in sys.argv
calc_only = "--calc-only" in sys.argv
positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
DEFAULT_QUESTION = "小明买了 1234 个零件，单价 56.78 元，商家打 8.5 折。请计算：原价总额是多少？折后总额是多少？"
question = positional[0] if positional else DEFAULT_QUESTION

# =========================================================================
# 1) 工具说明书 —— 给模型看的（和 _02 相同的结构，换了工具名和参数）
# =========================================================================
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "计算一个算术表达式并返回精确结果。支持 + - * / // % ** 和括号，"
                "例如 '1234*56.78'、'(100+20)/3'。涉及任何算术时都必须用本工具，不要心算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的算术表达式，例如 '1234*56.78'",
                    }
                },
                "required": ["expression"],
            },
        },
    }
]


# =========================================================================
# 2) 工具实现 —— 安全计算器
#    绝不用内置 eval() 执行模型生成的字符串：那是把任意代码执行权交给模型。
#    这里先 ast.parse 成语法树，再按白名单逐节点求值，非白名单语法直接拒绝。
# =========================================================================
_binary_ops = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_unary_ops = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_MAX_POW_EXPONENT = 100  # 防止 9**9**9 这类巨量计算拖垮进程


def _eval_node(node: ast.AST) -> int | float:
    """递归求值语法树节点；只有白名单内的节点类型能通过。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"不支持的常量：{node.value!r}")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _binary_ops:
            raise ValueError("不支持的运算符")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type is ast.Pow and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError(f"指数超出上限 {_MAX_POW_EXPONENT}")
        return _binary_ops[op_type](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _unary_ops:
        return _unary_ops[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"不支持的语法：{type(node).__name__}")


def calculate(expression: str) -> dict:
    """计算一个算术表达式。非法语法抛异常，由 execute_tool 转成结构化错误。"""
    tree = ast.parse(expression, mode="eval")
    return {"expression": expression, "result": _eval_node(tree.body)}


# =========================================================================
# 3) 工具注册表 + 本地执行分发（和 _02 相同：名字 → 函数，异常转错误结果）
# =========================================================================
tool_functions = {
    "calculate": calculate,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """执行工具；失败转成结构化错误回给模型（错误也是给模型的输入）。"""
    if name not in tool_functions:
        return {"error": f"未知工具：{name}"}
    try:
        return tool_functions[name](**arguments)
    except Exception as exc:
        return {"error": f"工具执行失败：{exc}"}


# =========================================================================
# 4) --calc-only 分支：只测计算器本身（不调模型、不需要 key）
#    走的是和主循环完全相同的 execute_tool 路径，保证行为一致。
# =========================================================================
if calc_only:
    expression = positional[0] if positional else "2+3*4"
    result = execute_tool("calculate", {"expression": expression})
    if "error" in result:
        print(f"[计算器] {expression} → 失败：{result['error']}")
    else:
        print(f"[计算器] {expression} = {result['result']}")
    raise SystemExit(0)

# =========================================================================
# 5) 读取项目根目录 .env 里的 API 配置（与前两个脚本相同）
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
        "只想验证计算器工具的话，加上 --calc-only \"2+3*4\" 运行（不需要 key）。\n"
    )

# =========================================================================
# 6) 主循环：模型 → 工具调用 → 模型 …… 直到模型不再申请工具
# =========================================================================
print("计算器工具循环演示")
print(f"问题：{question}")
print(f"工具模式：{'开启（提供 calculate）' if use_tools else '关闭（--no-tools 对照：模型只能心算）'}")
print("—" * 60)

system_prompt = (
    "你是一名严谨的中文助手。涉及任何算术计算时，必须先调用 calculate 工具来计算，不要自己心算。"
    "最终回答用中文，简洁，给出准确的数字。"
)
if not use_tools:
    system_prompt = "你是一名严谨的中文助手。最终回答用中文，简洁，给出准确的数字。"

messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": question},
]

client = OpenAI(api_key=api_key, base_url=base_url)
max_rounds = 6

model_calls = 0
tool_executions = 0
total_prompt_tokens = 0
total_completion_tokens = 0

for round_no in range(1, max_rounds + 1):
    model_calls += 1
    print(f"\n[模型第 {round_no} 次调用] 发出 {len(messages)} 条消息 + "
          f"{len(tools_schema) if use_tools else 0} 个工具说明书")

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        tools=tools_schema if use_tools else None,
    )
    if response.usage:
        total_prompt_tokens += response.usage.prompt_tokens
        total_completion_tokens += response.usage.completion_tokens

    message = response.choices[0].message

    # —— 循环的出口：模型不再申请工具，就是最终回答 ——
    if not message.tool_calls:
        print(f"  → 返回：最终回答（finish_reason={response.choices[0].finish_reason!r}，没有申请工具）")
        print("\n════════ 最终回答 ════════")
        print(message.content)
        break

    # —— 模型申请了工具：先把申请本身存回历史，再逐个执行 ——
    print(f"  → 返回：申请调用 {len(message.tool_calls)} 个工具（注意：只是申请，还没执行）")
    messages.append(
        {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ],
        }
    )

    for call in message.tool_calls:
        tool_executions += 1
        name = call.function.name
        arguments = json.loads(call.function.arguments)
        print(f"[执行第 {tool_executions} 次] {name}({json.dumps(arguments, ensure_ascii=False)})")

        result = execute_tool(name, arguments)
        if "error" in result:
            print(f"                 → 失败：{result['error']}（错误也会回填给模型，让它自己应对）")
        else:
            print(f"                 → 结果：{result['result']}")

        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    print("  → 结果已回填，继续下一轮")
else:
    print(f"\n达到最大轮数 {max_rounds} 仍未得出最终回答，已停止。")

# =========================================================================
# 7) 运行统计
# =========================================================================
print("\n════════ 本次运行统计 ════════")
print(f"模型调用：{model_calls} 次")
print(f"工具执行：{tool_executions} 次")
print(f"token 合计：prompt={total_prompt_tokens}  completion={total_completion_tokens}")
print("提示：模型调用次数 = 工具申请轮数 + 1（最后一轮给出最终回答）；")
print("      prompt token 逐轮增长，因为模型无状态，每次都要重发完整历史。")
