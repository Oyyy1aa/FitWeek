[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repositoryRoot
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
$environmentFile = Join-Path $repositoryRoot ".env"

if (-not (Test-Path $python)) {
    Write-Error "FitWeek virtual environment Python was not found."
    exit 1
}
if (-not (Test-Path $environmentFile)) {
    Write-Error "Local .env was not found."
    exit 1
}

foreach ($line in Get-Content $environmentFile) {
    if ($line -match "^\s*(TEST_DATABASE_URL|TEST_REDIS_URL)=(.*)$") {
        Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
    }
}
foreach ($name in "TEST_DATABASE_URL", "TEST_REDIS_URL") {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        Write-Error "$name is missing from local .env."
        exit 1
    }
}

# Local process tests must never inherit a developer proxy for loopback HTTP.
[Environment]::SetEnvironmentVariable("ALL_PROXY", $null)
[Environment]::SetEnvironmentVariable("all_proxy", $null)
[Environment]::SetEnvironmentVariable("HTTP_PROXY", $null)
[Environment]::SetEnvironmentVariable("http_proxy", $null)
[Environment]::SetEnvironmentVariable("HTTPS_PROXY", $null)
[Environment]::SetEnvironmentVariable("https_proxy", $null)
[Environment]::SetEnvironmentVariable("NO_PROXY", "127.0.0.1,localhost")
[Environment]::SetEnvironmentVariable("no_proxy", "127.0.0.1,localhost")

$baseTemp = Join-Path $repositoryRoot ".pytest-basetemp-integration-all-$PID"
New-Item -ItemType Directory -Force -Path $baseTemp | Out-Null
& $python -m pytest -m integration -q "--basetemp=$baseTemp"
exit $LASTEXITCODE
