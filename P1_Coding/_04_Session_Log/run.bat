@echo off
chcp 65001 >nul
rem One-click launcher for _04_Session_Log.
rem Double-click: full offline demo (no API key needed, FakeLLM).
rem Args:
rem   run.bat list                  forward to cli.py list
rem   run.bat show <id>             forward to cli.py show
rem   run.bat run <id> "q" --fake   forward to cli.py run
rem   run.bat --keep                keep the sessions/ demo dir
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

if "%~1"=="list"  goto :cli
if "%~1"=="show"  goto :cli
if "%~1"=="run"   goto :cli
if "%~1"=="fork"  goto :cli

"%PY%" "%~dp0demo.py" %*
goto :done

:cli
"%PY%" "%~dp0cli.py" %*

:done
echo.
pause
