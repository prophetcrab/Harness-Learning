#!/usr/bin/env bash
# _02_Tool_Calling 一键运行（Git Bash）：
#   bash run.sh                    完整链路（模型自主搜索，默认问题）
#   bash run.sh "你的问题"          完整链路（指定问题）
#   bash run.sh --search-only "词"  只测搜索工具，不调模型
#   bash run.sh inspect "你的问题"  逐次打印每次模型调用的请求与返回
cd "$(dirname "$0")" || exit 1

PROJECT_ROOT="$(cd ../.. && pwd)"
PY="$PROJECT_ROOT/.venv/Scripts/python.exe"

if [ ! -f "$PY" ]; then
    echo "[错误] 找不到虚拟环境 Python：$PY" >&2
    echo "请参考项目根目录 README.md 重建 .venv 环境。" >&2
    exit 1
fi

if [ "${1:-}" = "inspect" ]; then
    shift
    exec "$PY" inspect_model_calls.py "$@"
fi

if [ "$#" -eq 0 ]; then
    printf '请输入问题（直接回车使用默认问题）：'
    read -r QUESTION || QUESTION=""
    if [ -n "$QUESTION" ]; then
        exec "$PY" tool_calling.py "$QUESTION"
    fi
fi

exec "$PY" tool_calling.py "$@"
