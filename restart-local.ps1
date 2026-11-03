$ErrorActionPreference = 'Stop'
& "$PSScriptRoot/.venv/Scripts/python.exe" "$PSScriptRoot/scripts/local.py" restart
if ($LASTEXITCODE -ne 0) { throw 'Local restart failed; inspect .local logs.' }
