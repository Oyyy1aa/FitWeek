[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repositoryRoot
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "FitWeek virtual environment Python was not found."
    exit 1
}
if (-not (Test-Path ".env")) {
    Write-Error "Local .env was not found."
    exit 1
}
if (-not (Select-String -Path ".env" -Pattern "^\s*REDIS_URL=" -Quiet)) {
    Write-Error "REDIS_URL is missing from local .env."
    exit 1
}

& $python --version
& $python -c "import redis; print(f'redis-py: {redis.__version__}')"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$target = & $python -c "from urllib.parse import urlsplit; from app.config import Settings; parsed=urlsplit(Settings().redis_url.get_secret_value()); print(f'{parsed.hostname} {parsed.port or 6379}')"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($target)) {
    Write-Error "Could not read Redis host and port from local configuration."
    exit 1
}
$redisHost, $redisPort = $target.Trim().Split(" ")
if (-not (Test-NetConnection -ComputerName $redisHost -Port ([int]$redisPort) -InformationLevel Quiet)) {
    Write-Error "Redis TCP connectivity check failed."
    exit 1
}
Write-Output "Redis TCP: PASS"

& $python ".\scripts\check_existing_redis.py"
exit $LASTEXITCODE
