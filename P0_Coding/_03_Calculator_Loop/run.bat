@echo off
chcp 65001 >nul
rem One-click launcher for _03_Calculator_Loop.
rem Double-click: run the loop with the default question.
rem Args:
rem   run.bat "your question"          run the loop with your question
rem   run.bat --no-tools "question"    contrast run: no tools, model does mental math
rem   run.bat --calc-only "2+3*4"      calculator tool only, no model needed
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
"%PY%" "%~dp0calculator_loop.py" %*
goto :done

:with_question
"%PY%" "%~dp0calculator_loop.py" "%QUESTION%"
goto :done

:default
"%PY%" "%~dp0calculator_loop.py"

:done
echo.
pause
