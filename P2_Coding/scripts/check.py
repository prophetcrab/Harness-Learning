"""本地门禁：ruff + pytest（对应学习计划硬约束 #3「门禁不绿不前进」）。

P2 是**阶段式工作区**：`_01`–`_04` 各自自包含（每个目录带一份完整 `harness/`）。
所以门禁按阶段隔离执行，避免同名顶层包互相干扰：

- ruff：整个 P2_Coding 跑一次（配置见 pyproject.toml）；
- pytest：逐阶段在各自目录里跑（每个阶段独立 rootdir）；
- mypy：若已安装，对每个阶段的 `harness` 再跑一遍（当前环境未装则跳过）。

用法（在 P2_Coding 目录下）：

    python scripts/check.py            # 跑全部检查
    python scripts/check.py --fix      # 先让 ruff 自动修可修的问题，再跑
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # P2_Coding
PY = sys.executable
STAGES = [
    "_01_Prompt_Sections",
    "_02_Prompt_Context",
    "_03_Prompt_Trace",
    "_04_Filesystem_Seam",
    "_05_Workspace_Jail",
    "_06_Subprocess_Seam",
    "_07_Plugin_Effect",
    "_08_Profile_Layers",
    "_09_Dump_Config",
    "_10_Rpc_Transport",
    "_11_Session_Follow",
]


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
    for stage in STAGES:
        results.append(run(f"pytest {stage}", [PY, "-m", "pytest"], ROOT / stage))
        if mypy:
            results.append(run(f"mypy {stage}", [mypy, "harness"], ROOT / stage))
    if mypy is None:
        print("\n[跳过] 未找到 mypy（可选；ruff + pytest 已是本阶段门禁）")

    print("\n" + "=" * 48)
    all_ok = all(results)
    print("门禁总评：" + ("全绿 ✓" if all_ok else "失败 ✗（修到全绿再前进）"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
