#!/usr/bin/env bash
# _01_LLM_Calling 一键运行（Git Bash）：bash run.sh；也可带问题：bash run.sh "你的问题"
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
        exec "$PY" llm_calling.py "$QUESTION"
    fi
fi

exec "$PY" llm_calling.py "$@"
