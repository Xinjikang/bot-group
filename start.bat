@echo off
setlocal
rem Native Windows start entry. Python runs tools/service.py.
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY="%~dp0.venv\Scripts\python.exe""
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [start-engine] Python not found. Install Python 3.11+ and retry.
  exit /b 1
)
%PY% tools\service.py start
exit /b %errorlevel%
