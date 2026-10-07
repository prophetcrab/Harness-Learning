#!/usr/bin/env bash
# _04_Session_Log 一键运行（Git Bash）：
#   bash run.sh            全套演示（离线，不需要 key）
#   bash run.sh --keep     保留 sessions/ 演示目录
#   bash run.sh list       转发给 cli.py（list/show/run/fork）
#   bash run.sh run <id> "问题" [--fake]
cd "$(dirname "$0")" || exit 1

PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"

if [ ! -f "$PY" ]; then
    echo "[错误] 找不到虚拟环境 Python：$PY" >&2
    echo "请参考项目根目录 README.md 重建 .venv 环境。" >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    exec "$PY" demo.py
fi

case "$1" in
    list|show|run|fork)
        exec "$PY" cli.py "$@"
        ;;
    *)
        exec "$PY" demo.py "$@"
        ;;
esac
