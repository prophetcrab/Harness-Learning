"""pytest 引导：让本练习的包在"全量测试"下也不被同名包顶掉。

背景：_02/_03/_04/_05 各自带一份同名顶层包（agent_loop / llm_seam / tool_pipeline /
session 等）。Python 的 sys.modules 按名字缓存，先被导入的那份会固化，导致别的
练习 import 到错误版本（连子模块 agent_loop.loop 也会被缓存，须连同前缀一起驱逐）。

做法：本目录的 conftest 在该目录的测试被导入之前执行——清掉这些包及其所有子模块，
再把本目录放到 sys.path 最前。这样本练习的测试只会拿到自己目录下的包；单跑某
练习时同样无副作用。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 各练习目录里的同名顶层包；连同其子模块（foo.bar）一并驱逐，防止缓存串味。
_PACKAGES = (
    "agent_loop",
    "llm_seam",
    "tool_pipeline",
    "session",
    "runner",
    "mini_harness",
    "workspace_tools",
    "web_search",
    "env",
    "app",
)
for _key in [k for k in sys.modules if k.split(".")[0] in _PACKAGES]:
    sys.modules.pop(_key, None)
