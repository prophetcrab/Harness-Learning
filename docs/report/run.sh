#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
  exec "$ROOT/.venv/Scripts/python.exe" server.py "$@"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  exec "$ROOT/.venv/bin/python" server.py "$@"
else
  exec python server.py "$@"
fi
