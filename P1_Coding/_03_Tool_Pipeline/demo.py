"""_03_Tool_Pipeline 的入口脚本：演示工具执行管线的四条路径。

运行方式：
    python demo.py                 真实调用 DeepSeek（需要项目根 .env 里的 key）
    python demo.py --fake          离线剧本（FakeLLM），不联网、不需要 key
    python demo.py --pipeline-only 只演示管线本身（不调模型，不需要 key）

--pipeline-only 会把 pre/execute/post 三段、审批三态、超时、截断逐个跑一遍，
是最快理解本练习的方式。
"""

import os
import sys
from pathlib import Path

from agent_loop import AgentLoop
from llm_seam import FakeLLM, text_reply, tool_call_reply
from llm_seam.deepseek import DeepSeekProvider
from tool_pipeline import (
    AutoApprove,
    AutoDeny,
    ScriptedApprover,
    ToolPipeline,
    TruncateOutput,
    build_default_registry,
)
from tool_pipeline.approval import ApprovalDecision

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # <- P1_Coding/_03/../../ = 项目根
WORKSPACE = Path(__file__).resolve().parent / "demo_workspace"

use_fake = "--fake" in sys.argv
pipeline_only = "--pipeline-only" in sys.argv
positional = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
DEFAULT_QUESTION = "帮我算 1234*56.78 是多少？"
question = positional[0] if positional else DEFAULT_QUESTION


# =========================================================================
# 1) --pipeline-only：不调模型，直接观察管线本身
# =========================================================================
if pipeline_only:
    print("工具执行管线演示（pre → execute → post）\n" + "—" * 60)
    WORKSPACE.mkdir(exist_ok=True)
    registry = build_default_registry(WORKSPACE)

    print("\n[1] 正常成功：calculate")
    pipeline = ToolPipeline(registry)
    print("   ", pipeline.run("calculate", {"expression": "1234*56.78"}).content)

    print("\n[2] 执行超时：sleep 2 秒，超时预算 0.3 秒")
    print("   ", ToolPipeline(registry, timeout=0.3).run("sleep", {"seconds": 2}).content)

    print("\n[3] post 截断：echo_long 造超长输出，被 TruncateOutput 截断")
    truncated = ToolPipeline(registry, middlewares=[TruncateOutput(max_chars=50)]).run(
        "echo_long", {"repeat": 20}
    )
    print("   ", {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v) for k, v in truncated.content.items()})

    print("\n[4] 审批三态：write_file 需要审批")
    print("    4a 批准 →", ToolPipeline(registry, approval=ScriptedApprover([ApprovalDecision(True, "批准")])).run(
        "write_file", {"path": "note.txt", "content": "hello"}
    ).content)
    print("    4b 拒绝 →", ToolPipeline(registry, approval=AutoDeny("用户拒绝")).run(
        "write_file", {"path": "note.txt", "content": "hello"}
    ).content)
    print("    4c 无审批策略（fail-closed）→",
          ToolPipeline(registry, approval=None).run("write_file", {"path": "note.txt", "content": "x"}).content)

    print("\n[5] 沙箱越界：write_file 写 '../escape.txt'，审批放行也拒绝")
    print("   ", ToolPipeline(registry, approval=AutoApprove()).run(
        "write_file", {"path": "../escape.txt", "content": "x"}
    ).content)

    print("\n[6] 工作区沙箱正在真正生效：note.txt 是否落盘 →",
          (WORKSPACE / "note.txt").exists())
    raise SystemExit(0)


# =========================================================================
# 2) 组装：注册表 → 管线 → AgentLoop
# =========================================================================
WORKSPACE.mkdir(exist_ok=True)
registry = build_default_registry(WORKSPACE)
system_prompt = (
    "你是一名严谨的中文助手，可以调用工具。涉及算术计算时必须调用 calculate，不要心算。"
    "最终回答用中文，简洁。"
)

if use_fake:
    provider = FakeLLM(
        [
            tool_call_reply("calculate", {"expression": "1234*56.78"}),
            text_reply("1234 × 56.78 = 70066.52。（来自 FakeLLM 剧本）"),
        ]
    )
    approval = AutoApprove()
    mode_name = "FakeLLM（离线剧本，不需要 key）"
else:
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
            "离线体验请加 --fake 或 --pipeline-only（不需要 key）。"
        )
    provider = DeepSeekProvider(
        api_key=api_key,
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    # 真实模式下 write_file 审批改为终端 y/n（这里演示用 AutoApprove 会自动放行，
    # 想手动体验审批把下面一行换成 PromptApprover()）。
    approval = AutoApprove()
    mode_name = "DeepSeekProvider（真实 API）"


def show_event(kind: str, payload: dict) -> None:
    if kind == "step_request":
        print(f"\n[step {payload['step']}] 请求模型：{payload['message_count']} 条消息 + {payload['tool_count']} 个工具")
    elif kind == "step_response":
        calls = payload["tool_calls"]
        print(f"           ← {'申请调用 ' + str(calls) if calls else '最终回答'}")
    elif kind == "tool_result":
        print(f"           → {payload['name']}({payload['arguments']}) 结果：{payload['result']}")


print(f"provider：{mode_name}")
print(f"审批策略：{type(approval).__name__}")
print(f"问题：{question}")

pipeline = ToolPipeline(registry, approval=approval, timeout=30.0)
loop = AgentLoop(
    provider=provider,
    execute_tool=pipeline.execute,  # ← 管线通过执行器契约插进主循环
    system_prompt=system_prompt,
    max_steps=6,
    on_event=show_event,
)

result = loop.run(question, tools=registry.specs())

print("\n════════ 最终回答 ════════")
print(result.final_text)
print(f"\n状态：{result.status}    模型调用（step）：{len(result.steps)} 次")

if isinstance(provider, FakeLLM):
    provider.assert_all_consumed()
    print("剧本正好用完 ✓")
