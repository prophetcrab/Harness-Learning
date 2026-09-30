"""_02_Tool_Calling 附加实验：逐次打印每一次模型调用的"请求摘要 + 原始返回"。

和同目录 tool_calling.py 的分工：
- tool_calling.py   展示完整的工具调用流程（模型申请 → 本地执行 → 回填 → 再请求）
- 本脚本           把流程中"每一次模型调用"原样摊开：
                    发过去哪些消息、模型返回的 finish_reason / content / tool_calls 到底长什么样

用法：
    ../../.venv/Scripts/python.exe inspect_model_calls.py
    ../../.venv/Scripts/python.exe inspect_model_calls.py "今天上海天气怎么样？"
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_QUESTION = "今天上海天气怎么样？"
question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION

# =========================================================================
# 1) 工具说明书（与 tool_calling.py 相同）
# =========================================================================
tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "用关键词进行网络搜索，返回标题、链接和摘要。适合查询实时信息或你不确定的事实。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，例如 '上海今天天气'",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

# =========================================================================
# 2) 工具实现（与 tool_calling.py 相同）
# =========================================================================
def parse_bing_results(page_html: str, max_results: int) -> list[dict]:
    """从 Bing 搜索结果页里提取标题 / 链接 / 摘要。"""
    results = []
    for chunk in page_html.split('<li class="b_algo"')[1:]:
        title_match = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S
        )
        if not title_match:
            continue
        title = unescape(re.sub(r"<[^>]+>", "", title_match.group(2))).strip()
        url = unescape(title_match.group(1))

        snippet = ""
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        if snippet_match:
            snippet = unescape(re.sub(r"<[^>]+>", "", snippet_match.group(1))).strip()

        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def web_search(query: str, max_results: int = 5) -> dict:
    """调用 Bing 完成一次真实搜索。"""
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        page_html = response.read().decode("utf-8", errors="ignore")
    return {"query": query, "results": parse_bing_results(page_html, max_results)}


# =========================================================================
# 3) 工具注册表（与 tool_calling.py 相同）
# =========================================================================
tool_functions = {
    "web_search": web_search,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """按名字执行工具；异常转成结构化错误回给模型。"""
    if name not in tool_functions:
        return {"error": f"未知工具：{name}"}
    try:
        return tool_functions[name](**arguments)
    except Exception as exc:
        return {"error": f"工具执行失败：{exc}"}


# =========================================================================
# 4) 打印辅助 —— 本脚本的重点：把"发出去什么"和"收回来什么"看清
# =========================================================================
def show_request(round_no: int, messages: list) -> None:
    """打印本次请求摘要：模型这一轮将看到哪些消息、各是什么角色。"""
    print(f"\n════════════ 第 {round_no} 次模型调用 ════════════")
    print(f"[请求] 发出 {len(messages)} 条消息 + {len(tools_schema)} 个工具说明书：")
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "assistant" and message.get("tool_calls"):
            names = ", ".join(call["function"]["name"] for call in message["tool_calls"])
            print(f"  [{index}] assistant   （含 tool_calls: {names}）")
        elif role == "tool":
            content = str(message.get("content") or "")
            print(f"  [{index}] tool        {len(content)} 字  tool_call_id={message['tool_call_id']}")
        else:
            content = str(message.get("content") or "")
            preview = content[:36].replace("\n", " ")
            print(f"  [{index}] {role:<9} {len(content)} 字   {preview}…")


def show_reply(response) -> None:
    """打印模型返回的完整结构 —— 看 tool_calls 的真实模样。"""
    choice = response.choices[0]
    message = choice.message

    print(f"[返回] finish_reason   = {choice.finish_reason!r}   ← 'tool_calls'=要调工具 / 'stop'=最终回答")
    print(f"       message.content = {message.content!r}")

    if message.tool_calls:
        print(f"       message.tool_calls：{len(message.tool_calls)} 个申请（注意：这只是申请，还没执行）")
        for index, call in enumerate(message.tool_calls):
            print(f"         [{index}] id        = {call.id}")
            print(f"              type      = {call.type}")
            print(f"              name      = {call.function.name}")
            print(f"              arguments = {call.function.arguments!r}")
            print(f"              # arguments 是 JSON 字符串（不是字典），回填时要 json.loads 解析")
    else:
        print("       message.tool_calls = None   ← 模型没有申请工具")

    usage = response.usage
    if usage:
        print(f"       本轮 token：prompt={usage.prompt_tokens}  completion={usage.completion_tokens}")


# =========================================================================
# 5) 读取项目根目录 .env 里的 API 配置（与 _01 相同）
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
    )

# =========================================================================
# 6) 主循环：每一轮都完整打印"请求摘要 + 原始返回"
# =========================================================================
print("本实验：完整打印 tool calling 流程中每一次模型调用")
print(f"问题：{question}")
print("—" * 60)

system_prompt = (
    "你是一名严谨的中文助手，可以调用工具。"
    "涉及实时信息、事实核查或你不确定的内容时，必须先调用 web_search 查询，再基于搜索结果回答。"
    "搜索一次即可；只有结果明显不相关时才换个关键词再搜一次。"
    "搜索结果不充分时要如实说明，不要编造。最终回答用中文，简洁，并给出来源链接。"
)

messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": question},
]

client = OpenAI(api_key=api_key, base_url=base_url)
max_rounds = 5
usage_log = []  # (轮次, prompt_tokens, completion_tokens)，最后汇总对比

for round_no in range(1, max_rounds + 1):
    show_request(round_no, messages)

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        tools=tools_schema,
    )
    show_reply(response)

    message = response.choices[0].message
    if response.usage:
        usage_log.append((round_no, response.usage.prompt_tokens, response.usage.completion_tokens))

    if not message.tool_calls:
        print("\n════════════ 模型给出最终回答，流程结束 ════════════")
        print(message.content)
        break

    # 把 assistant 消息转成"线上格式"的字典存回历史。
    # 下一轮模型靠这条消息"记得"自己申请过什么；真实 harness 里这条就是要落日志的事件。
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
        name = call.function.name
        arguments = json.loads(call.function.arguments)
        print(f"\n[执行] 现在才真正执行：{name}({json.dumps(arguments, ensure_ascii=False)})")

        result = execute_tool(name, arguments)
        results = result.get("results", [])
        first_title = results[0]["title"] if results else "(无结果)"
        print(f"       执行完毕，返回 {len(results)} 条，第 1 条标题：{first_title}")

        # 结果以 role="tool" 回填；tool_call_id 与申请配对，模型才知道这是哪次申请的结果
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
else:
    print(f"\n达到最大轮数 {max_rounds} 仍未得出最终回答，已停止。")

# =========================================================================
# 7) 收尾汇总：对比各轮的 token 消耗，验证"完整历史每次都要重发"
# =========================================================================
if usage_log:
    print("\n════════════ 各次调用的 token 消耗 ════════════")
    print("  轮次    prompt(输入)    completion(输出)")
    for round_no, prompt_tokens, completion_tokens in usage_log:
        print(f"    {round_no}        {prompt_tokens:>8}         {completion_tokens:>8}")
    print("prompt tokens 逐轮增长 —— 因为每次调用都要把完整对话历史重新发过去（模型无状态）。")
    print("这就是真实 harness 必须记录每次请求的原因：\"模型可见⟺已记录\"。")
