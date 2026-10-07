"""P3_Coding 的 pytest 引导：让 tests/ 能 import 到同级的 harness 包。

tests/ 在 P3_Coding/tests/，包在 P3_Coding/harness/。这里把 P3_Coding 放进
sys.path，`import harness.*` 即可解析，无需 pip install（环境里没有 .venv）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # P3_Coding
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
