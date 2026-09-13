param(
    [switch]$Rebuild,
    [switch]$Logs
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail '未找到 Docker CLI。请先安装 Docker Desktop，并确保 docker 命令已加入 PATH。'
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail 'Docker CLI 已找到，但 Docker Desktop/daemon 未运行。请启动 Docker Desktop 后重试。'
}

if (-not (Test-Path -LiteralPath '.env')) {
    if (-not (Test-Path -LiteralPath '.env.example')) {
        Fail '缺少 .env.example，无法生成环境文件。'
    }
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host '已根据 .env.example 创建 .env。'
    Write-Host '请先编辑 .env，填写 MYSQL_PASSWORD、MYSQL_ROOT_PASSWORD、LLM_MODEL 和 LLM_API_KEY。'
    exit 2
}

$envLines = Get-Content -LiteralPath '.env'
function Get-EnvValue([string]$Name) {
    $line = $envLines | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -First 1
    if ($null -eq $line) { return '' }
    return (($line -split '=', 2)[1]).Trim().Trim([char]34)
}

$missing = @()
foreach ($name in @('MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_ROOT_PASSWORD')) {
    if ([string]::IsNullOrWhiteSpace((Get-EnvValue $name))) {
        $missing += $name
    }
}

foreach ($name in @('LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY')) {
    $value = Get-EnvValue $name
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match 'replace-with|change-me') {
        $missing += $name
    }
}

if ($missing.Count -gt 0) {
    Write-Host ' .env 中缺少或仍使用占位值的配置：' -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host ('  - ' + $_) -ForegroundColor Yellow }
    Write-Host '请编辑 .env 后重新运行 .\start.ps1。' -ForegroundColor Yellow
    exit 2
}

Write-Host '正在校验 Docker Compose 配置...'
docker compose config --quiet
if ($LASTEXITCODE -ne 0) {
    Fail 'Docker Compose 配置校验失败，请根据上面的错误修正 .env 或 compose.yaml。'
}

$composeArgs = @('up', '-d')
if ($Rebuild -or -not (docker image inspect recordings-api:local 2>$null)) {
    $composeArgs += '--build'
}

Write-Host '正在启动 MySQL、迁移、API 和 Worker...'
docker compose @composeArgs
if ($LASTEXITCODE -ne 0) {
    Fail '服务启动失败。可运行 docker compose logs --tail 100 api worker migrate 查看日志。'
}

Write-Host '启动完成：API 文档 http://127.0.0.1:8000/docs' -ForegroundColor Green
if ($Logs) {
    docker compose logs -f --tail 100 api worker
}
