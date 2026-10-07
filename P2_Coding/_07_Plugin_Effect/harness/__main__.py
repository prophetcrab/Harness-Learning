"""python -m harness 的入口：转交给 harness.cli。

用法：
    python -m harness chat --session s1
    python -m harness run "帮我算 2+3"
    python -m harness list
"""

from __future__ import annotations

import sys

from harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
