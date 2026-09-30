#!/usr/bin/env bash
# _01_Provider_Protocol 一键运行（Git Bash）：
#   bash run.sh                 真实 API 调用（默认问题）
#   bash run.sh --fake          离线剧本 FakeLLM（不需要 key）
#   bash run.sh "你的问题"       真实 API 调用（指定问题）
#   bash run.sh --fake "问题"    离线剧本（剧本固定，仅演示）
cd "$(dirname "$0")" || exit 1

PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"

if [ ! -f "$PY" ]; then
    echo "[错误] 找不到虚拟环境 Python：$PY" >&2
    echo "请参考项目根目录 README.md 重建 .venv 环境。" >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    printf '请输入问题（直接回车使用默认问题）：'
    read -r QUESTION || QUESTION=""
    if [ -n "$QUESTION" ]; then
        exec "$PY" demo.py "$QUESTION"
    fi
fi

exec "$PY" demo.py "$@"
