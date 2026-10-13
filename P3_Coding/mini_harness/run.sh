#!/usr/bin/env bash
# One-click launcher for P3 coding package (fully pluggable mini harness).
#   bash run.sh                    offline demo (FakeLLM + MemoryFS + scripted shell)
#   bash run.sh chat               offline interactive chat
#   bash run.sh chat --realtime    real combination (DeepSeek + disk + real shell)
#   bash run.sh test               run the test suite
set -euo pipefail
cd "$(dirname "$0")"

PY="../../.venv/Scripts/python.exe"
if [ ! -f "$PY" ]; then
  PY="../../.venv/bin/python"
fi
if [ ! -f "$PY" ]; then
  PY="python"
fi

case "${1:-}" in
  "")
    "$PY" demo.py
    ;;
  chat)
    shift
    "$PY" chat.py "$@"
    ;;
  test)
    "$PY" -m pytest -q
    ;;
  *)
    "$PY" demo.py "$@"
    ;;
esac
