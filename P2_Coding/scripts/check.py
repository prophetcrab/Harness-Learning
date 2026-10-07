"""本地门禁：ruff + pytest（对应学习计划硬约束 #3 "门禁不绿不前进"）。

用法（在 P2_Coding 目录下）：

    python scripts/check.py            # 跑全部检查
    python scripts/check.py --fix      # 先让 ruff 自动修可修的问题，再跑

设计要点：**pytest 不是可选项**——ruff 只是先跑；任何一步非零退出，整体即失败。
mypy 若已安装则一并运行（当前环境未装，则跳过并提示）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # P2_Coding
PY = sys.executable


def run(name: str, cmd: list[str]) -> bool:
    print(f"\n=== {name} ===\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT)
    ok = result.returncode == 0
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} ---")
    return ok


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    targets = ["harness", "demo.py", "tests", "scripts"]

    ruff = shutil.which("ruff")
    if ruff is None:
        print("[跳过] 未找到 ruff CLI（pip install ruff 后可启用 lint 门禁）")
        ruff_ok = True
    else:
        if fix:
            run("ruff fix", [ruff, "check", "--fix", *targets])
        ruff_ok = run("ruff", [ruff, "check", *targets])

    pool_ok = run("pytest", [PY, "-m", "pytest"])

    mypy = shutil.which("mypy")
    if mypy is None:
        print("\n[跳过] 未找到 mypy（可选；ruff + pytest 已是本阶段门禁）")
        mypy_ok = True
    else:
        mypy_ok = run("mypy", [mypy, "harness"])

    print("\n" + "=" * 48)
    all_ok = ruff_ok and pool_ok and mypy_ok
    print("门禁总评：" + ("全绿 ✓" if all_ok else "失败 ✗（修到全绿再前进）"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
