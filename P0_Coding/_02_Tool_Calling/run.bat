@echo off
chcp 65001 >nul
rem One-click launcher for _02_Tool_Calling.
rem Double-click: full tool-calling loop with the default question.
rem Args:
rem   run.bat "your question"           full loop with your question
rem   run.bat --search-only "keywords"  search tool only, no model
rem   run.bat inspect "your question"   print every model call request/reply
cd /d "%~dp0"

set "PY=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    echo Please rebuild the .venv environment - see README.md in the project root.
    pause
    exit /b 1
)

if /i "%~1"=="inspect" goto :inspect
if not "%~1"=="" goto :with_args

set "QUESTION="
set /p QUESTION=Question [Enter for default]: 
if not defined QUESTION goto :default
goto :with_question

:with_args
"%PY%" "%~dp0tool_calling.py" %*
goto :done

:with_question
"%PY%" "%~dp0tool_calling.py" "%QUESTION%"
goto :done

:default
"%PY%" "%~dp0tool_calling.py"
goto :done

:inspect
if "%~2"=="" (
    "%PY%" "%~dp0inspect_model_calls.py"
) else (
    "%PY%" "%~dp0inspect_model_calls.py" "%~2"
)

:done
echo.
pause
