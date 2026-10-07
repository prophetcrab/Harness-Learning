@echo off
chcp 65001 >nul
rem One-click launcher for _02_Agent_Loop.
rem Double-click: real DeepSeek API call with the default question.
rem Args:
rem   run.bat --fake            offline scripted FakeLLM demo (no API key needed)
rem   run.bat "your question"   real API call with your question
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    echo Please rebuild the .venv environment - see README.md in the project root.
    pause
    exit /b 1
)

if not "%~1"=="" goto :with_args

set "QUESTION="
set /p QUESTION=Question [Enter for default]: 
if not defined QUESTION goto :default
goto :with_question

:with_args
"%PY%" "%~dp0demo.py" %*
goto :done

:with_question
"%PY%" "%~dp0demo.py" "%QUESTION%"
goto :done

:default
"%PY%" "%~dp0demo.py"

:done
echo.
pause
