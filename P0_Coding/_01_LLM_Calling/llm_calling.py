"""_01_LLM_Calling —— 最小化模型调用 + 提示词拼装（直接运行的单文件脚本）。

用法：
    ../../.venv/Scripts/python.exe llm_calling.py
    ../../.venv/Scripts/python.exe llm_calling.py "你的问题"

脚本从上到下分 6 节，读一遍就是一次模型调用的完整流程：
    1. 提示词小节（数据） 2. 运行时变量 3. 装配 system 提示词
    4. 读取 .env 配置     5. 组装 messages  6. 调用模型并流式打印
"""

import os
import platform
import sys
from datetime import date
from pathlib import Path

from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

# 本文件位于 <项目根>/P0_Coding/_01_LLM_Calling/llm_calling.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# =========================================================================
# 1) 提示词小节：想改人设、环境说明、输出要求，改这里
#    每个小节独立命名，内容里可以写 {{变量}} 占位符，最后按顺序拼接。
# =========================================================================
sections = [
    (
        "角色设定",
        "你是一名严谨的中文技术助手。回答必须准确、简洁，不确定的地方要明说。",
    ),
    (
        "工作环境",
        "当前日期：{{today}}\n操作系统：{{platform}}\n学习项目目录：{{project_root}}",
    ),
    (
        "输出要求",
        "用中文回答。先给一句话结论，然后最多列 3 条要点。不要编造不确定的事实。",
    ),
]

# =========================================================================
# 2) 运行时变量：装配时才取值，不写死在模板里
# =========================================================================
variables = {
    "today": date.today().isoformat(),
    "platform": f"{platform.system()} {platform.release()}",
    "project_root": str(PROJECT_ROOT),
}

# =========================================================================
# 3) 装配 system 提示词：逐节替换 {{变量}}，再按注册顺序拼接
# =========================================================================
parts = []
for name, template in sections:
    text = template
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    if "{{" in text:  # fail loud：绝不把带占位符的提示词发给模型
        raise ValueError(f"小节 [{name}] 中存在未解析的变量")
    parts.append(f"[{name}]\n{text}")
system_prompt = "\n\n".join(parts)

# =========================================================================
# 4) 读取项目根目录 .env 里的 API 配置（不覆盖已有的系统环境变量）
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
# 5) 组装 messages：system 装装配好的提示词，user 只放本次问题
# =========================================================================
question = sys.argv[1] if len(sys.argv) > 1 else "用一句话解释什么是 agent harness。"
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": question},
]

print("=== 装配后的 system 提示词 ===")
print(system_prompt)
print("\n=== 本次问题 ===")
print(question)

if not api_key:
    raise SystemExit(
        "\n未找到 DEEPSEEK_API_KEY，无法调用模型。\n"
        "请在项目根目录 .env 中写入：DEEPSEEK_API_KEY=sk-...\n"
    )

# =========================================================================
# 6) 调用模型：流式接收，逐段打印
# =========================================================================
print("\n=== 模型回答 ===")
client = OpenAI(api_key=api_key, base_url=base_url)
response = client.chat.completions.create(model=model_name, messages=messages, stream=True)

for chunk in response:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
