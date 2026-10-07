#!/usr/bin/env bash
# mini harness 可视化页面一键启动（Git Bash）。
# 直接调用真实 API（读取项目根 .env 里的 DEEPSEEK_API_KEY），默认端口 8765。
#   bash run_web.sh                      真实 API，默认端口
#   bash run_web.sh --port 9000 --no-open
#   bash run_web.sh --search             额外启用 web_search 工具
#   bash run_web.sh --deny-writes        禁止一切需审批的写操作
cd "$(dirname "$0")" || exit 1

PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"
if [ ! -f "$PY" ]; then
    echo "[提示] 未找到 .venv，回退到 PATH 上的 python。" >&2
    PY="python"
fi

exec "$PY" server.py "$@"
