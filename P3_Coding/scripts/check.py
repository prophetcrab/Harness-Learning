"""本地门禁：ruff + pytest（对应学习计划硬约束 #3「门禁不绿不前进」）。

P3 是**阶段式工作区**：`mini_harness/` 是一个自包含的基础包
（kernel + components + plugins 三层），因此门禁只有一处：

- ruff：整个 P3_Coding 跑一次（配置见 pyproject.toml）；
- pytest：在 mini_harness/ 里跑（自己一个 rootdir，conftest 负责同名包隔离）；
- mypy：若已安装，对 kernel / components / plugins 再跑一遍（未装则跳过）。

用法（在 P3_Coding 目录下）：

    python scripts/check.py            # 跑全部检查
    python scripts/check.py --fix      # 先让 ruff 自动修可修的问题，再跑
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # P3_Coding
PY = sys.executable
PACKAGES = ["mini_harness"]


def run(name: str, cmd: list[str], cwd: Path) -> bool:
    where = "." if cwd == ROOT else cwd.name
    print(f"\n=== {name} ===\n$ {' '.join(cmd)}   (cwd={where})")
    ok = subprocess.run(cmd, cwd=cwd).returncode == 0
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} ---")
    return ok


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    results: list[bool] = []

    ruff = shutil.which("ruff")
    if ruff is None:
        print("[跳过] 未找到 ruff CLI（pip install ruff 后可启用 lint 门禁）")
        results.append(True)
    else:
        if fix:
            run("ruff fix", [ruff, "check", "--fix", "."], ROOT)
        results.append(run("ruff", [ruff, "check", "."], ROOT))

    mypy = shutil.which("mypy")
    for package in PACKAGES:
        if not (ROOT / package).is_dir():
            continue
        results.append(run(f"pytest {package}", [PY, "-m", "pytest"], ROOT / package))
        if mypy:
            for layer in ("kernel", "components", "plugins"):
                results.append(run(f"mypy {package}/{layer}", [mypy, layer], ROOT / package))
    if mypy is None:
        print("\n[跳过] 未找到 mypy（可选；ruff + pytest 已是本阶段门禁）")

    print("\n" + "=" * 48)
    all_ok = all(results)
    print("门禁总评：" + ("全绿 ✓" if all_ok else "失败 ✗（修到全绿再前进）"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
