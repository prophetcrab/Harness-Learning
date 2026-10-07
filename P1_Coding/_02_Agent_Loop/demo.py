"""_02_Agent_Loop 的入口脚本：把 _01 的闭环升级成正式的 AgentLoop 类。

运行方式：
    python demo.py                # 真实调用 DeepSeek（需要项目根 .env 里的 key）
    python demo.py --fake         # 离线剧本（FakeLLM），不联网、不需要 key
    python demo.py "你的问题"      # 换问题（fake 模式的剧本是固定的，会提示这一点）

与 _01 的差异：这里跑的是 agent_loop.AgentLoop —— 一次 run() 是一个 turn，
turn 内部每轮模型往返是一个 step。结束时打印 turn/step 轨迹，让你直观看到
"一个 turn 里有多个 step"这个此前被扁平字段淹没的事实。
"""

import json
import os
import sys
from pathlib import Path

from agent_loop import AgentLoop
from llm_seam import (
    FakeLLM,
    build_default_toolbox,
    text_reply,
    tool_call_reply,
)
from llm_seam.deepseek import DeepSeekProvider

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # <- P1_Coding/_02_../../ = 项目根

# =========================================================================
# 1) 参数：--fake 用离线剧本；其余位置参数是问题
# =========================================================================
use_fake = "--fake" in sys.argv
positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
DEFAULT_QUESTION = "帮我算 987654 * 321，再把结果除以 7 等于多少？"
question = positional[0] if positional else DEFAULT_QUESTION

# =========================================================================
# 2) 工具箱与系统提示词（两种模式共用）
# =========================================================================
toolbox = build_default_toolbox()
system_prompt = (
    "你是一名严谨的中文助手，可以调用工具。"
    "涉及算术计算时必须调用 calculate 工具，不要心算。"
    "最终回答用中文，简洁。"
)

# =========================================================================
# 3) ★ 唯一的差异点：构造 provider ★
#    —— 换供应商只改这一处；下面的 AgentLoop 对两者完全一视同仁。
# =========================================================================
if use_fake:
    # 离线剧本：一次 turn 内两次工具申请 + 一次最终回答（共 3 个 step）。
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "987654*321"}),
            tool_call_reply("calculate", {"expression": "317036934/7"}),
            text_reply("987654 × 321 = 317036934，再除以 7 = 45290990.57…（来自 FakeLLM 剧本）"),
        ]
    )
    mode_name = "FakeLLM（离线剧本，不需要 key）"
else:
    # 读取项目根目录 .env 里的配置（与 P0/_01 脚本相同的做法）
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
# 4) 事件回调：把循环的每一轮打印成人能读的日志（观察者模式）
#    循环自身不打印 —— 打印是"观察者"的事，轨迹则通过返回值拿。
# =========================================================================
def show_event(kind: str, payload: dict) -> None:
    if kind == "turn_start":
        print(f"\n── turn {payload['turn']} 开始：{payload['user']}")
    elif kind == "step_request":
        print(
            f"   [step {payload['step']}] 请求模型："
            f"{payload['message_count']} 条消息 + {payload['tool_count']} 个工具"
        )
    elif kind == "step_response":
        calls = payload["tool_calls"]
        if calls:
            print(f"                ← 回复：申请调用 {calls}（还没执行）")
        else:
            print(f"                ← 回复：最终回答（finish_reason={payload['finish_reason']}）")
    elif kind == "tool_result":
        status = "失败" if payload["is_error"] else "成功"
        arguments = json.dumps(payload["arguments"], ensure_ascii=False)
        result = json.dumps(payload["result"], ensure_ascii=False)
        print(f"                → 执行 {payload['name']}({arguments}) {status}：{result}")
    elif kind == "turn_end":
        print(f"   ── turn {payload['turn']} 结束：status={payload['status']}，共 {payload['steps']} 个 step")


# =========================================================================
# 5) 跑一个 turn：注意这一节没有一行 provider 专属代码
# =========================================================================
print(f"provider：{mode_name}")
print(f"问题：{question}")
if use_fake and question != DEFAULT_QUESTION:
    print("（剧本模式演示的是固定剧本：两次 calculate + 一次回答，与提问内容无关）")

loop = AgentLoop(
    provider=provider,
    execute_tool=toolbox.execute,
    system_prompt=system_prompt,
    max_steps=6,
    on_event=show_event,
)

result = loop.run(question, tools=toolbox.specs())

# =========================================================================
# 6) 结果与轨迹
# =========================================================================
print("\n════════ 最终回答 ════════")
print(result.final_text)

print(f"\n状态：{result.status}    本次 turn：{result.turn}    模型调用（step）：{len(result.steps)} 次")
print("\n════════ 调用轨迹 ════════")
for step in result.steps:
    request_roles = "、".join(m.role for m in step.request.messages)
    calls = step.response.message.tool_calls
    if calls:
        action = f"申请 {[c.name for c in calls]}，执行 {len(step.tool_results)} 次"
    else:
        action = "最终回答"
    print(
        f"  step {step.index}：请求 {len(step.request.messages)} 条消息（{request_roles}）"
        f" → {action}"
    )
print(f"  共 1 个 turn，内含 {len(result.steps)} 个 step（turn 与 step 是两个层次）。")

if isinstance(provider, FakeLLM):
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
