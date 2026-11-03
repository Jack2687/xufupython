param([switch]$Database)
$ErrorActionPreference = 'Stop'
if ($Database) {
    & "$PSScriptRoot/.venv/Scripts/python.exe" "$PSScriptRoot/scripts/local.py" stop --database
} else {
    & "$PSScriptRoot/.venv/Scripts/python.exe" "$PSScriptRoot/scripts/local.py" stop
}
if ($LASTEXITCODE -ne 0) { throw 'Local stop failed; inspect .local logs.' }
