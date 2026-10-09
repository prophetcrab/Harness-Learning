@echo off
chcp 65001 >nul
rem One-click launcher for _02_Prompt_Context.
rem   run.bat                  offline demo (FakeLLM, no API key)
rem   run.bat chat             real-API chat with per-step rendered prompt
rem   run.bat chat --fake      offline chat (no API key)
rem   run.bat chat --ask "hi"  one-shot question
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

"%PY%" -m harness %*

:done
echo.
pause
