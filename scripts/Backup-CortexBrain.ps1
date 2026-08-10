param(
    [ValidateSet("Full", "Bank")]
    [string]$Type = "Full"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backupDirectory = Join-Path $projectRoot "backups"
$null = New-Item -ItemType Directory -Path $backupDirectory -Force
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$adminPath = Join-Path $env:APPDATA "uv\tools\hindsight-embed\Scripts\hindsight-admin.exe"

if (-not (Test-Path -LiteralPath $adminPath)) {
    throw "hindsight-admin.exe was not found. Run Install-CortexBrain.ps1 first."
}

# Wake the service-owned database first. Otherwise pg0 can start PostgreSQL
# from this interactive backup shell and recreate the terminal-window problem.
& (Join-Path $PSScriptRoot "Start-CortexBrain.ps1") | Out-Null

$env:HINDSIGHT_API_DATABASE_URL = "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight"
$env:UV_PYTHON = "3.12"
if ($Type -eq "Full") {
    $destination = Join-Path $backupDirectory "hindsight-cortex-$stamp.zip"
    & $adminPath backup $destination
}
else {
    $destination = Join-Path $backupDirectory "cortex-bank-$stamp.zip"
    & $adminPath export-bank --bank cortex --output $destination --include-history
}

if ($LASTEXITCODE -ne 0) {
    throw "Backup failed with exit code $LASTEXITCODE."
}

Write-Output $destination
