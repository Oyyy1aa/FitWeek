[CmdletBinding()]
param(
    [string]$AdminUser = "root"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$myIniPath = "C:\Study\Java\Mysql\my.ini"
$mysqlPath = "C:\Program Files\MySQL\MySQL Server 9.4\bin\mysql.exe"
$envPath = Join-Path $projectRoot ".env"

function Get-MySqlSetting {
    param([string]$Name, [string]$Content)
    $section = ""
    foreach ($line in $Content -split "`r?`n") {
        if ($line -match '^\s*\[(?<section>[^\]]+)\]') {
            $section = $Matches.section.ToLowerInvariant()
            continue
        }
        if ($section -eq "mysqld" -and $line -match "^\s*$([regex]::Escape($Name))\s*=\s*(?<value>[^#;\s]+)") {
            return $Matches.value.Trim('"')
        }
    }
    return $null
}

function Get-EnvironmentValue {
    param([string]$Name)
    if (-not (Test-Path -LiteralPath $envPath)) { return $null }
    $match = Get-Content -LiteralPath $envPath | Where-Object {
        $_ -match "^$([regex]::Escape($Name))="
    } | Select-Object -First 1
    if ($null -eq $match) { return $null }
    return $match.Substring($Name.Length + 1)
}

function New-LocalPassword {
    $alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return -join (1..32 | ForEach-Object { $alphabet[(Get-Random -Maximum $alphabet.Length)] })
}

function Invoke-MySql {
    param([string]$Password, [string]$Sql)
    & $mysqlPath --protocol=tcp --host=127.0.0.1 --port=$port --user=$AdminUser --password=$Password --batch --skip-column-names --execute=$Sql
    if ($LASTEXITCODE -ne 0) { throw "MySQL command failed." }
}

function Test-FitweekConnection {
    param([string]$ConnectionUrl, [string]$DatabaseName)
    $uri = [System.Uri]$ConnectionUrl
    $parts = $uri.UserInfo.Split(":", 2)
    if ($parts.Count -ne 2) { throw "The local connection URL has no password." }
    $user = [uri]::UnescapeDataString($parts[0])
    $password = [uri]::UnescapeDataString($parts[1])
    & $mysqlPath --protocol=tcp --host=127.0.0.1 --port=$port --user=$user --password=$password --database=$DatabaseName --execute="SELECT 1;" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "FitWeek account connection verification failed." }
}

$service = Get-Service -Name "MySQL94" -ErrorAction SilentlyContinue
if ($null -eq $service) { throw "MySQL94 service was not found." }
if ($service.Status -ne "Running") { throw "MySQL94 is not running; this script will not start or stop it." }
if (-not (Test-Path -LiteralPath $myIniPath)) { throw "my.ini was not found at $myIniPath" }
if (-not (Test-Path -LiteralPath $mysqlPath)) { throw "mysql.exe was not found at $mysqlPath" }

$myIni = Get-Content -Raw -LiteralPath $myIniPath
$port = Get-MySqlSetting -Name "port" -Content $myIni
if ([string]::IsNullOrWhiteSpace($port)) { throw "The MySQL port could not be read from [mysqld] in my.ini." }

$adminPassword = Read-Host "MySQL administrator password for $AdminUser" -AsSecureString
$bstr = [IntPtr]::Zero
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($adminPassword)
    $adminPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    Invoke-MySql -Password $adminPlain -Sql "SELECT VERSION();" | Out-Null

    $databaseUrl = Get-EnvironmentValue -Name "DATABASE_URL"
    $testDatabaseUrl = Get-EnvironmentValue -Name "TEST_DATABASE_URL"
    if ($databaseUrl -or $testDatabaseUrl) {
        Write-Host "Existing DATABASE_URL/TEST_DATABASE_URL values were preserved."
    } else {
        $appPassword = New-LocalPassword
        $testPassword = New-LocalPassword
        Invoke-MySql -Password $adminPlain -Sql @"
CREATE DATABASE IF NOT EXISTS fitweek CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS fitweek_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'fitweek_app'@'127.0.0.1' IDENTIFIED BY '$appPassword';
CREATE USER IF NOT EXISTS 'fitweek_test'@'127.0.0.1' IDENTIFIED BY '$testPassword';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, TRIGGER, EVENT ON fitweek.* TO 'fitweek_app'@'127.0.0.1';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, TRIGGER, EVENT ON fitweek_test.* TO 'fitweek_test'@'127.0.0.1';
FLUSH PRIVILEGES;
"@
        $encodedApp = [uri]::EscapeDataString($appPassword)
        $encodedTest = [uri]::EscapeDataString($testPassword)
        @(
            "APP_MODE=single_user",
            "PERSISTENCE_BACKEND=mysql",
            "APP_HOST=127.0.0.1",
            "DATABASE_URL=mysql+asyncmy://fitweek_app:$encodedApp@127.0.0.1:$port/fitweek?charset=utf8mb4",
            "TEST_DATABASE_URL=mysql+asyncmy://fitweek_test:$encodedTest@127.0.0.1:$port/fitweek_test?charset=utf8mb4"
        ) | Set-Content -LiteralPath $envPath -Encoding utf8
        $databaseUrl = Get-EnvironmentValue -Name "DATABASE_URL"
        $testDatabaseUrl = Get-EnvironmentValue -Name "TEST_DATABASE_URL"
    }

    Test-FitweekConnection -ConnectionUrl $databaseUrl -DatabaseName "fitweek"
    Test-FitweekConnection -ConnectionUrl $testDatabaseUrl -DatabaseName "fitweek_test"
    Write-Host "MySQL94 setup succeeded: fitweek and fitweek_test on port $port. Passwords were not displayed."
}
finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    Remove-Variable adminPlain -ErrorAction SilentlyContinue
}
