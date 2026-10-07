"""_04_Filesystem_Seam 的 pytest 引导。

1) 把本阶段目录放进 sys.path，让 `import harness.*` 解析到本阶段自带的 harness 副本；
2) 全量跑（pytest P0_Coding P1_Coding P2_Coding）时，驱逐已缓存的 `harness` 包，
   避免别的阶段（或 P1 `_05`）先导入的那份固化在 sys.modules 里导致串味。
   单跑本阶段无副作用。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _key in [k for k in sys.modules if k == "harness" or k.startswith("harness.")]:
    sys.modules.pop(_key, None)
