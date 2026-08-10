param(
    [string]$OutputDirectory = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $projectRoot ("backups\cortex-migration-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force

# The exact baseline uses a loopback-only Hindsight API and the legacy source
# bank id "cortex". This script performs no source-bank mutation.
& (Join-Path $PSScriptRoot "Start-CortexBrain.ps1") | Out-Null
$health = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8888/health" -TimeoutSec 30
if ($health.status -ne "healthy" -or $health.database -ne "connected") {
    throw "Cortex source Hindsight health check failed closed."
}

$bankBackupOutput = @(& (Join-Path $PSScriptRoot "Backup-CortexBrain.ps1") -Type Bank)
$bankBackupExitCode = $LASTEXITCODE
$bankBackup = if ($bankBackupOutput.Count -gt 0) { $bankBackupOutput[-1] } else { $null }
if (
    $bankBackupExitCode -ne 0 -or
    $bankBackup -isnot [string] -or
    [string]::IsNullOrWhiteSpace($bankBackup) -or
    -not (Test-Path -LiteralPath $bankBackup -PathType Leaf)
) {
    throw "Source bank backup did not complete."
}
Copy-Item -LiteralPath $bankBackup -Destination $OutputDirectory -Force

$fullBackupOutput = @(& (Join-Path $PSScriptRoot "Backup-CortexBrain.ps1") -Type Full)
$fullBackupExitCode = $LASTEXITCODE
$fullBackup = if ($fullBackupOutput.Count -gt 0) { $fullBackupOutput[-1] } else { $null }
if (
    $fullBackupExitCode -ne 0 -or
    $fullBackup -isnot [string] -or
    [string]::IsNullOrWhiteSpace($fullBackup) -or
    -not (Test-Path -LiteralPath $fullBackup -PathType Leaf)
) {
    throw "Full source backend backup did not complete."
}
Copy-Item -LiteralPath $fullBackup -Destination $OutputDirectory -Force

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -eq $python) {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
}
if ($null -eq $python) {
    throw "Python is required to create the deterministic Cortex migration export."
}

$inventory = Join-Path $OutputDirectory "windows-cortex-inventory.json"
$memories = Join-Path $OutputDirectory "windows-cortex-memories.jsonl"
if ($python.Name -eq "py.exe") {
    & $python.Source -3.12 (Join-Path $projectRoot "src\cortex\migration.py") --url "http://127.0.0.1:8888" inventory --bank cortex | Set-Content -LiteralPath $inventory -Encoding UTF8
    & $python.Source -3.12 (Join-Path $projectRoot "src\cortex\migration.py") --url "http://127.0.0.1:8888" export --bank cortex --source windows-cortex --output $memories
}
else {
    & $python.Source (Join-Path $projectRoot "src\cortex\migration.py") --url "http://127.0.0.1:8888" inventory --bank cortex | Set-Content -LiteralPath $inventory -Encoding UTF8
    & $python.Source (Join-Path $projectRoot "src\cortex\migration.py") --url "http://127.0.0.1:8888" export --bank cortex --source windows-cortex --output $memories
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $memories)) {
    throw "Deterministic source export failed."
}

$files = Get-ChildItem -LiteralPath $OutputDirectory -File | Sort-Object Name
$bankBackupName = Split-Path -Leaf $bankBackup
$fullBackupName = Split-Path -Leaf $fullBackup
$manifest = [ordered]@{
    schema_version = 1
    createdAtUtc = [DateTime]::UtcNow.ToString("o")
    migration_source = "windows-cortex"
    product = "Cortex"
    engine = "Hindsight 0.8.4"
    api_version = "0.8.4"
    source_bank = "cortex"
    sourceApi = "127.0.0.1:8888"
    required_artifacts = [ordered]@{
        memory_export = "windows-cortex-memories.jsonl"
        inventory_export = "windows-cortex-inventory.json"
        native_bank_backup = $bankBackupName
        full_backend_backup = $fullBackupName
    }
    files = @($files | ForEach-Object {
        [ordered]@{
            name = $_.Name
            bytes = $_.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        }
    })
}
$manifestPath = Join-Path $OutputDirectory "SHA256SUMS.json"
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

# Verify every byte before declaring the export usable.
$verified = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
foreach ($item in $verified.files) {
    $path = Join-Path $OutputDirectory $item.name
    if (-not (Test-Path -LiteralPath $path)) { throw "Backup file missing: $path" }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ((Get-Item -LiteralPath $path).Length -ne $item.bytes -or $actual -ne $item.sha256) {
        throw "Backup verification failed: $path"
    }
}

Write-Output $OutputDirectory
