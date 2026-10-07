@echo off
chcp 65001 >nul
rem One-click launcher for the mini harness visualization page.
rem Double-click: offline demo provider (no API key needed) on http://127.0.0.1:8765
rem Args:
rem   run_web.bat              offline demo provider (no API key)
rem   run_web.bat --fake       same as above (explicit)
rem   run_web.bat             (real API: edit this file to drop --fake)
rem   run_web.bat --port 9000 --no-open
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [INFO] .venv not found, falling back to "python" on PATH.
    set "PY=python"
)

if "%~1"=="" (
    "%PY%" "%~dp0server.py" --fake
    goto :done
)

"%PY%" "%~dp0server.py" %*

:done
echo.
pause
