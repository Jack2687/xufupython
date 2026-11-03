$ErrorActionPreference = 'Stop'
& "$PSScriptRoot/.venv/Scripts/python.exe" "$PSScriptRoot/scripts/local.py" start
if ($LASTEXITCODE -ne 0) { throw 'Local start failed; inspect .local logs.' }
