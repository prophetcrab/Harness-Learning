#!/usr/bin/env bash
# _05_Mini_Harness 一键运行（Git Bash）：
#   bash run.sh                离线 demo（FakeLLM，不需要 key）
#   bash run.sh chat           真实 API 交互对话
#   bash run.sh chat --fake    离线剧本交互对话
#   bash run.sh list           列出已落盘的会话
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

exec "$PY" app.py "$@"
