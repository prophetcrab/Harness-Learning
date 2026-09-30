"""_01_Provider_Protocol 的入口脚本：同一个闭环，两种 provider。

运行方式：
    python demo.py                # 真实调用 DeepSeek（需要项目根 .env 里的 key）
    python demo.py --fake         # 离线剧本（FakeLLM），不联网、不需要 key
    python demo.py "你的问题"      # 换问题（fake 模式的剧本是固定的，会提示这一点）

本脚本存在的意义：证明"换 provider 只改一行" —— 唯一的差异在第 3 节
构造 provider 的地方；第 5 节的闭环调用对两者完全一视同仁。
"""

import json
import os
import sys
from pathlib import Path

from llm_seam import (
    FakeLLM,
    Toolbox,
    build_default_toolbox,
    run_tool_loop,
    system,
    text_reply,
    tool_call_reply,
    user,
)
from llm_seam.deepseek import DeepSeekProvider

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # <- P1_Coding/_01_../../ = 项目根

# =========================================================================
# 1) 参数：--fake 用离线剧本；其余位置参数是问题
# =========================================================================
use_fake = "--fake" in sys.argv
positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
DEFAULT_QUESTION = "帮我算一下 1234 * 56.78 等于多少？"
question = positional[0] if positional else DEFAULT_QUESTION

# =========================================================================
# 2) 工具箱与系统提示词（两种模式共用）
# =========================================================================
toolbox: Toolbox = build_default_toolbox()
system_prompt = (
    "你是一名严谨的中文助手，可以调用工具。"
    "涉及算术计算时必须调用 calculate 工具，不要心算。"
    "最终回答用中文，简洁。"
)

# =========================================================================
# 3) ★ 唯一的差异点：构造 provider ★
#    —— 换供应商只改这一处；下面的闭环代码对两者完全一视同仁。
# =========================================================================
if use_fake:
    # 离线剧本：先申请一次计算，再基于结果作答。
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52。（来自 FakeLLM 的剧本回答）"),
        ]
    )
    mode_name = "FakeLLM（离线剧本，不需要 key）"
else:
    # 读取项目根目录 .env 里的配置（与 P0 脚本相同的做法）
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "未找到 DEEPSEEK_API_KEY。\n"
            "配置：项目根目录 .env 写入 DEEPSEEK_API_KEY=sk-...\n"
            "离线体验请加 --fake 参数（不需要 key）。"
        )
    provider = DeepSeekProvider(
        api_key=api_key,
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    mode_name = "DeepSeekProvider（真实 API）"


# =========================================================================
# 4) 事件回调：把闭环的每一轮打印成人能读的日志
#    闭环通过 on_event 暴露内部过程，自身不打印 —— 打印是"观察者"的事。
# =========================================================================
def show_event(kind: str, payload: dict) -> None:
    if kind == "request":
        print(
            f"\n[第 {payload['step']} 步] 请求模型："
            f"{payload['message_count']} 条消息 + {payload['tool_count']} 个工具"
        )
    elif kind == "response":
        calls = payload["tool_calls"]
        if calls:
            print(f"           ← 回复：申请调用 {calls}（还没执行）")
        else:
            print(f"           ← 回复：最终回答（finish_reason={payload['finish_reason']}）")
    elif kind == "tool_result":
        status = "失败" if payload["is_error"] else "成功"
        arguments = json.dumps(payload["arguments"], ensure_ascii=False)
        result = json.dumps(payload["result"], ensure_ascii=False)
        print(f"           → 执行 {payload['name']}({arguments}) {status}：{result}")


# =========================================================================
# 5) 跑闭环：注意这一节没有一行 provider 专属代码
# =========================================================================
print(f"provider：{mode_name}")
print(f"问题：{question}")
if use_fake and question != DEFAULT_QUESTION:
    print("（剧本模式演示的是固定剧本：一次 calculate 调用 + 一次回答，与提问内容无关）")

result = run_tool_loop(
    provider=provider,
    messages=[system(system_prompt), user(question)],
    tools=toolbox.specs(),
    execute_tool=toolbox.execute,
    max_steps=6,
    on_event=show_event,
)

# =========================================================================
# 6) 结果与统计
# =========================================================================
print("\n════════ 最终回答 ════════")
print(result.final_text)
print(f"\n状态：{result.status}    模型调用：{result.steps} 次")

if isinstance(provider, FakeLLM):
    # 请求记录平时是给测试断言用的，这里打印出来让你直接观察"模型看到了什么"
    print("\nFakeLLM 请求记录：")
    for index, request in enumerate(provider.requests, start=1):
        roles = "、".join(message.role for message in request.messages)
        print(f"  第 {index} 次：{len(request.messages)} 条消息（{roles}），{len(request.tools)} 个工具")
    provider.assert_all_consumed()
    print("  剧本正好用完 ✓（循环既没有多调也没有少调）")

print(
    f"\n提示：本次 provider 是 {mode_name.split('（')[0]}；"
    "切换成另一种只需改脚本第 3 节的一行构造。"
)
