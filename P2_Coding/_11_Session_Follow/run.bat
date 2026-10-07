@echo off
chcp 65001 >nul
rem One-click launcher for _11_Session_Follow.
rem   run.bat                 offline demo (FakeLLM, no API key)
rem   run.bat chat --fake     interactive chat (offline scripted provider)
rem   run.bat list            list stored sessions
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if "%~1"=="" (
    "%PY%" "%~dp0demo.py"
    goto :done
)
"%PY%" -m harness %*

:done
echo.
pause
