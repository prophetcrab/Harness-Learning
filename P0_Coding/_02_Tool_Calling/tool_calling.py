"""_02_Tool_Calling —— 最小化的工具调用 + 调用结果展示（单文件平铺脚本）。

学习目标：让模型从"只会说"变成"会先查再答"。
一次完整的工具调用链路：

    模型看到工具说明书 → 决定调用 web_search(参数) → 脚本真正执行搜索
    → 把结果作为 tool 消息回填 → 模型根据结果给出最终回答

用法：
    ../../.venv/Scripts/python.exe tool_calling.py
    ../../.venv/Scripts/python.exe tool_calling.py "上海今天的天气怎么样？"
    ../../.venv/Scripts/python.exe tool_calling.py --search-only "python 教程"   # 只测搜索，不调模型、不需要 key
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

# 命令行参数：--search-only 只跑搜索；其余位置参数当作要问的问题
search_only = "--search-only" in sys.argv
positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
question = positional[0] if positional else "上海今天的天气怎么样？"

# =========================================================================
# 1) 工具定义 —— 给模型看的"说明书"
#    JSON Schema 只描述工具名、用途和参数；真正干什么由下面的 Python 函数决定，
#    两者靠"名字"对应。模型看到的永远只有这份说明书，看不到你的代码。
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
# 2) 工具实现 —— 真正干活的函数
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
    """调用 Bing 完成一次真实搜索。失败时抛异常，由 execute_tool 统一转成错误结果。"""
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
# 3) 工具注册表 + 本地执行分发
#    模型说"调用 web_search"，这里按名字找到对应函数并执行 ——
#    这就是 harness 里工具注册表（学习计划 M2）的最小形态。
# =========================================================================
tool_functions = {
    "web_search": web_search,
}


def execute_tool(name: str, arguments: dict) -> dict:
    """执行工具，并把异常转成结构化错误 —— 错误也是给模型的输入（铁律 #7）。"""
    if name not in tool_functions:
        return {"error": f"未知工具：{name}"}
    try:
        return tool_functions[name](**arguments)
    except Exception as exc:  # 任何失败都回给模型，而不是中断整个循环
        return {"error": f"工具执行失败：{exc}"}


# =========================================================================
# 4) 调用结果展示 —— 把工具返回的内容打印成人能读的样子
# =========================================================================
def show_results(result: dict) -> None:
    if "error" in result:
        print(f"[工具结果] 出错：{result['error']}")
        return
    results = result.get("results", [])
    print(f"[工具结果] 关键词「{result.get('query', '')}」共 {len(results)} 条：")
    for index, item in enumerate(results, 1):
        print(f"  {index}. {item['title']}")
        print(f"     {item['url']}")
        if item["snippet"]:
            snippet = item["snippet"]
            if len(snippet) > 100:
                snippet = snippet[:100] + "…"
            print(f"     摘要：{snippet}")


# =========================================================================
# 5) --search-only 分支：只验证搜索本身（不调模型，不需要 key）
# =========================================================================
if search_only:
    print(f"[搜索] {question}")
    show_results(web_search(question))
    raise SystemExit(0)

# =========================================================================
# 6) 读取项目根目录 .env 里的 API 配置（与 _01 相同）
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

# =========================================================================
# 7) 主循环：模型 → 工具 → 模型 …… 直到模型不再请求工具
# =========================================================================
print("本轮提供给模型的工具：[web_search] 网络搜索")
print(f"问题：{question}\n")

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

if not api_key:
    raise SystemExit(
        "\n未找到 DEEPSEEK_API_KEY，无法调用模型。\n"
        "请在项目根目录 .env 中写入：DEEPSEEK_API_KEY=sk-...\n"
        "只想验证搜索工具的话，加上 --search-only 参数运行。\n"
    )

client = OpenAI(api_key=api_key, base_url=base_url)
max_rounds = 5  # 防止模型没完没了地调用工具

for round_no in range(1, max_rounds + 1):
    print(f"--- 第 {round_no} 轮：请求模型 ---")
    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        tools=tools_schema,
    )
    message = response.choices[0].message

    if not message.tool_calls:
        print("\n=== 最终回答 ===")
        print(message.content)
        break

    if message.content:
        print(f"[模型说明] {message.content}")

    # assistant 消息（含 tool_calls）要原样存入历史：模型下一轮必须"记得"自己请求过什么
    messages.append(message)

    for tool_call in message.tool_calls:
        name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)
        print(f"[工具调用] {name}({json.dumps(arguments, ensure_ascii=False)})")

        result = execute_tool(name, arguments)
        show_results(result)

        # 结果以 role="tool" 回填；tool_call_id 必须与请求一一配对，模型才知道这是哪次调用的结果
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    print("（已把工具结果回填给模型，进入下一轮）")
else:
    print(f"\n达到最大轮数 {max_rounds} 仍未得出最终回答，已停止。")
