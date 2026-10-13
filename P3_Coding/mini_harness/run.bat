@echo off
chcp 65001 >nul
rem One-click launcher for P3 coding package (fully pluggable mini harness).
rem   run.bat                    offline demo (FakeLLM + MemoryFS + scripted shell)
rem   run.bat chat               offline interactive chat
rem   run.bat chat --realtime    real combination (DeepSeek + disk + real shell)
rem   run.bat chat --ask "hi"    one-shot question (offline)
rem   run.bat web                web UI plugin (offline; --realtime for real API)
rem   run.bat test               run the test suite
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

if /i "%~1"=="web" (
    shift
    "%PY%" "%~dp0chat.py" --web %1 %2 %3 %4 %5 %6 %7 %8
    goto :done
)

if /i "%~1"=="test" (
    "%PY%" -m pytest -q
    goto :done
)

rem Anything else: pass through to demo.py
"%PY%" "%~dp0demo.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
goto :done

:done
