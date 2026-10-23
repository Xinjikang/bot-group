@echo off
setlocal
rem Bootstrap entry: detect Python and run tools/bootstrap.py.
rem Download, verify, extract and install use the Python standard library.
rem Set BOOTSTRAP_URL, BOOTSTRAP_SHA256 and optionally INSTALL_DIR.
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo [bootstrap] Python not found. Install Python 3.11+ and retry.
  exit /b 1
)

%PY% tools\bootstrap.py
exit /b %errorlevel%
