@echo off
chcp 65001 >nul
rem One-click launcher for the mini harness visualization page.
rem Uses the real DeepSeek API (reads DEEPSEEK_API_KEY from the project root .env).
rem Double-click: opens http://127.0.0.1:8765/
rem Args:
rem   run_web.bat                       real API, default port 8765
rem   run_web.bat --port 9000 --no-open
rem   run_web.bat --search              also enable the web_search tool
rem   run_web.bat --deny-writes         deny every approval-gated write
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [INFO] .venv not found, falling back to "python" on PATH.
    set "PY=python"
)

"%PY%" "%~dp0server.py" %*

echo.
pause
