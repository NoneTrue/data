$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path '.venv\Scripts\python.exe')) {
        & py -3.11 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 64-bit, then run setup again.' }
    }
    & .\.venv\Scripts\python.exe -m pip install --keyring-provider disabled --only-binary=:all: -r requirements-lock-windows.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & .\.venv\Scripts\python.exe -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
    Write-Host 'Ready. Run run_windows.bat, or .venv\Scripts\python.exe verify.py to test.'
} finally { Pop-Location }
