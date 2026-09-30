@echo off
chcp 65001 >nul
rem One-click launcher for _01_LLM_Calling.
rem Double-click to run, or pass a question: run.bat "your question"
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
"%PY%" "%~dp0llm_calling.py" %*
goto :done

:with_question
"%PY%" "%~dp0llm_calling.py" "%QUESTION%"
goto :done

:default
"%PY%" "%~dp0llm_calling.py"

:done
echo.
pause
