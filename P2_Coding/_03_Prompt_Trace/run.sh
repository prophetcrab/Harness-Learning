#!/usr/bin/env bash
# _02_Prompt_Context 一键运行（Git Bash）：
#   bash run.sh                 离线 demo（不需要 key）
#   bash run.sh chat            真实 API 对话（每 step 渲染系统提示词）
#   bash run.sh chat --fake     离线对话
#   bash run.sh chat --ask "..." 一条问题跑一个 turn
#   bash run.sh list            列会话（基线 CLI）
cd "$(dirname "$0")" || exit 1
PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"
[ -f "$PY" ] || PY="python"
[ "$#" -eq 0 ] && exec "$PY" demo.py
if [ "$1" = "chat" ]; then
    shift
    exec "$PY" chat.py "$@"
fi
exec "$PY" -m harness "$@"
