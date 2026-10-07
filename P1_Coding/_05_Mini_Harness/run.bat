@echo off
chcp 65001 >nul
rem One-click launcher for _05_Mini_Harness.
rem Double-click: offline demo (FakeLLM, no API key needed).
rem Args:
rem   run.bat                offline demo (no API key needed)
rem   run.bat chat           interactive chat with the real DeepSeek API
rem   run.bat chat --fake    interactive chat with the offline scripted FakeLLM
rem   run.bat list           list stored sessions
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    echo Please rebuild the .venv environment - see README.md in the project root.
    pause
    exit /b 1
)

if "%~1"=="" (
    "%PY%" "%~dp0demo.py"
    goto :done
)

"%PY%" "%~dp0app.py" %*

:done
echo.
pause
