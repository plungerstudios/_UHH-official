@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
for /d %%P in ("%LocalAppData%\Programs\Python\Python*") do (
  if exist "%%~fP\python.exe" set "PYTHON_EXE=%%~fP\python.exe"
)
if not defined PYTHON_EXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=py -3"
)
if not defined PYTHON_EXE (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
  echo Python 3 is required to build _UHH. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

%PYTHON_EXE% -m PyInstaller --noconfirm --clean --onefile --console --name _UHH ^
  --add-data "index.html;." ^
  --add-data "styles.css;." ^
  --add-data "app.js;." ^
  server.py
if errorlevel 1 (
  echo Build failed. Install PyInstaller with: %PYTHON_EXE% -m pip install pyinstaller
  pause
  exit /b 1
)

if exist backend_url.txt copy /Y backend_url.txt dist\backend_url.txt >nul
echo.
echo Built dist\_UHH.exe
echo If backend_url.txt was not present when building, put it next to _UHH.exe before launching.
pause
