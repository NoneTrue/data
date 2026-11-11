@echo off
setlocal
cd /d "%~dp0"
if defined CHANGEVISION_PYTHON (
  "%CHANGEVISION_PYTHON%" main.py
) else (
  if not exist ".venv\Scripts\python.exe" (
    echo Run setup_windows.ps1 first, or install requirements into a Python 3.11 environment.
    exit /b 1
  )
  ".venv\Scripts\python.exe" main.py
)
exit /b %errorlevel%
