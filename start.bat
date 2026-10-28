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
  pause
  exit /b 1
)
%PY% tools\service.py restart
set "START_EXIT=%errorlevel%"
if not "%START_EXIT%"=="0" (
  echo [start-engine] Startup failed. See the error above. Exit code: %START_EXIT%
  pause
)
exit /b %START_EXIT%
