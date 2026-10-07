#!/usr/bin/env bash
# mini harness 可视化页面一键启动（Git Bash）：
#   bash run_web.sh                 离线规则 provider（不需要 key），默认端口 8765
#   bash run_web.sh --fake          同上（显式）
#   bash run_web.sh                 去掉 --fake 即走真实 API（需要 .env 里的 key）
#   bash run_web.sh --port 9000 --no-open
cd "$(dirname "$0")" || exit 1

PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"
if [ ! -f "$PY" ]; then
    echo "[提示] 未找到 .venv，回退到 PATH 上的 python。" >&2
    PY="python"
fi

if [ "$#" -eq 0 ]; then
    exec "$PY" server.py --fake
fi

exec "$PY" server.py "$@"
