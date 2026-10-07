"""_01_Prompt_Sections 的 pytest 引导。

1) 把本阶段目录放进 sys.path：让 `import harness.*` 解析到**本阶段自带的 harness 副本**，
   让 `import prompt` 解析到**本阶段新增的顶层模块**（prompt/）。
2) 全量跑（pytest P0_Coding P1_Coding P2_Coding）时，驱逐这些**同名顶层包**已缓存的部分，
   避免别的阶段先导入的那份固化在 sys.modules 里导致串味。单跑本阶段无副作用。

驱逐的名字 = {基线包 harness} ∪ {各阶段的顶层新模块}。当前新增模块只有 _01 的 prompt；
后续阶段会新增 providers / config / server，届时一并加进来。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 需要"每次导入前清空"的同名顶层包（基线 + 各阶段新增模块）。
_PACKAGES = ("harness", "prompt", "providers", "config", "server")

for _key in [k for k in sys.modules if k.split(".")[0] in _PACKAGES]:
    sys.modules.pop(_key, None)
