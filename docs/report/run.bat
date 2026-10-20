@echo off
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0..\.."
if exist "%ROOT%\.venv\Scripts\python.exe" (
  "%ROOT%\.venv\Scripts\python.exe" server.py %*
) else (
  python server.py %*
)
