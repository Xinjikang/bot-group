@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [upgrade] Run the bot-group installer first.
  exit /b 1
)
set "BASE_PY="
for /f "delims=" %%P in ('.venv\Scripts\python.exe -c "import sys; print(sys._base_executable)"') do set "BASE_PY=%%P"
if not defined BASE_PY exit /b 1
"%BASE_PY%" -X utf8 scripts\upgrade.py upgrade %*
exit /b %errorlevel%
