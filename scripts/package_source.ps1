[CmdletBinding()]
param([string]$OutputPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "fitweek-source.zip"))

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$rootPath = [IO.Path]::GetFullPath($root)
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
if (-not $resolvedOutput.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar)) {
    throw "OutputPath must be inside the FitWeek workspace."
}
$include = @("app", "tests", "frontend", "migrations", "scripts", "docs", "README.md", "pyproject.toml", "alembic.ini", ".env.example", "docker-compose.yml", "Dockerfile")
$excluded = "(^|/)(\.venv[^/]*/|node_modules/|dist/|__pycache__/|\.pytest|\.mypy|\.ruff|\.env$|.*\.log$|.*\.ics$)"
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $resolvedOutput) { Remove-Item -LiteralPath $resolvedOutput -Force }
$archive = [System.IO.Compression.ZipFile]::Open($resolvedOutput, "Create")
try {
    foreach ($entry in $include) {
        $path = Join-Path $root $entry
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $path, $entry)
            continue
        }
        if (Test-Path -LiteralPath $path) {
            Get-ChildItem -LiteralPath $path -File -Recurse | ForEach-Object {
                $relative = $_.FullName.Substring($root.Length + 1).Replace("\\", "/")
                if ($relative -notmatch $excluded) {
                    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $_.FullName, $relative)
                }
            }
        }
    }
} finally { $archive.Dispose() }
Write-Host "Created $resolvedOutput"
