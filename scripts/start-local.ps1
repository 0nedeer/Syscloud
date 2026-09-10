param(
    [string]$Uv = 'uv',
    [string]$Python = '3.12',
    [ValidateRange(1, 65535)][int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.env')) {
        throw 'Create .env from .env.example and configure a dedicated MySQL database first.'
    }
    & $Uv sync --frozen --python $Python
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & '.\.venv\Scripts\python.exe' -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed; API was not started.' }
    & '.\.venv\Scripts\python.exe' -m uvicorn app.main:create_app --factory `
        --host 127.0.0.1 --port $Port --no-access-log
    if ($LASTEXITCODE -ne 0) { throw 'API process exited unsuccessfully.' }
}
finally {
    Pop-Location
}
