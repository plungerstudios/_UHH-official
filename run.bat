@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
for /d %%P in ("%LocalAppData%\Programs\Python\Python*") do (
  if exist "%%~fP\python.exe" set "PYTHON_EXE=%%~fP\python.exe"
)
if defined PYTHON_EXE (
  "%PYTHON_EXE%" server.py
  exit /b %errorlevel%
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 server.py
  exit /b %errorlevel%
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  python server.py
  exit /b %errorlevel%
)
echo Python 3 is required to run _UHH. Install it from https://www.python.org/downloads/
echo During setup, enable "Add python.exe to PATH".
pause
