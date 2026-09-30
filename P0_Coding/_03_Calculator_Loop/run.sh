#!/usr/bin/env bash
# _03_Calculator_Loop 一键运行（Git Bash）：
#   bash run.sh                      循环演示（默认问题）
#   bash run.sh "你的问题"            循环演示（指定问题）
#   bash run.sh --no-tools "问题"     对照实验：不给工具，模型只能心算
#   bash run.sh --calc-only "2+3*4"  只测计算器工具，不需要 key
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
        exec "$PY" calculator_loop.py "$QUESTION"
    fi
fi

exec "$PY" calculator_loop.py "$@"
