@echo off
chcp 65001 >nul
rem One-click launcher for _11_Session_Follow (P2 final stage).
rem   run.bat                  offline demo (FakeLLM, no API key)
rem   run.bat chat             real-API chat (config-driven: --profile dev|prod)
rem   run.bat chat --ask "hi"  one-shot question
rem   run.bat attach -s s1     follow client (spawns serve.py over stdio)
rem   run.bat attach --connect 8765 -s s1   follow a running TCP server
rem   run.bat serve            stdio JSON-RPC server
rem   run.bat serve --listen 8765          TCP server (multi-client)
rem   run.bat list             list stored sessions (baseline CLI)
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if "%~1"=="" (
    "%PY%" "%~dp0demo.py"
    goto :done
)

if /i "%~1"=="chat" (
    shift
    "%PY%" "%~dp0chat.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
    goto :done
)

if /i "%~1"=="serve" (
    shift
    "%PY%" "%~dp0serve.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
    goto :done
)

if /i "%~1"=="attach" (
    shift
    "%PY%" "%~dp0attach.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
    goto :done
)

"%PY%" -m harness %*

:done
echo.
pause
