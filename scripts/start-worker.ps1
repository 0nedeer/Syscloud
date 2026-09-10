$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & "$projectRoot/.venv/Scripts/python.exe" -m app.worker
    if ($LASTEXITCODE -ne 0) { throw 'Worker stopped with an error; see redacted logs.' }
} finally {
    Pop-Location
}
