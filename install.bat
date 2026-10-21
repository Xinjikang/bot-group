@echo off
setlocal
rem Install entry: install sets up dependencies; repair verifies an existing venv.
rem Safe to rerun. Failed setup stays unready. Git is optional.
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo [install] Python not found. Install Python 3.11+ and retry.
  exit /b 1
)

set "CMD=%~1"
if "%CMD%"=="" set "CMD=install"
%PY% scripts\install.py %CMD%
exit /b %errorlevel%
