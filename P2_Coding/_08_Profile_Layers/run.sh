#!/usr/bin/env bash
# _08_Profile_Layers 一键运行（Git Bash）：
#   bash run.sh              离线 demo（不需要 key）
#   bash run.sh chat --fake  离线交互对话
#   bash run.sh list         列会话
cd "$(dirname "$0")" || exit 1
PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"
[ -f "$PY" ] || PY="python"
[ "$#" -eq 0 ] && exec "$PY" demo.py
exec "$PY" -m harness "$@"
