#!/usr/bin/env bash
# mini_harness 一键运行（Git Bash）：
#   bash run.sh                    离线 demo（不需要 key）
#   bash run.sh chat [--profile prod] [--ask "..."]   配置驱动的对话
#   bash run.sh attach [-s s1]     跟随客户端（stdio：自己拉起 serve.py）
#   bash run.sh attach --connect 8765 -s s1            跟随已运行的 TCP 服务
#   bash run.sh serve [--listen 8765]                  JSON-RPC 服务
#   bash run.sh list               列会话（基线 CLI）
cd "$(dirname "$0")" || exit 1
PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"
[ -f "$PY" ] || PY="python"
[ "$#" -eq 0 ] && exec "$PY" demo.py
case "$1" in
    chat|serve|attach)
        cmd="$1"
        shift
        exec "$PY" "$cmd.py" "$@"
        ;;
    web)
        shift
        exec "$PY" webui/server.py "$@"
        ;;
esac
exec "$PY" -m harness "$@"
