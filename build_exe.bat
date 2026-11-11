@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.ps1 first.
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --keyring-provider disabled -r requirements-build.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm ChangeVision2026.spec
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" package_runtime.py dist\ChangeVision2026
exit /b %errorlevel%
