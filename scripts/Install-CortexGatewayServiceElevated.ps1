param(
    [int]$StartupTimeoutSeconds = 120
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot "runtime"
$installer = Join-Path $PSScriptRoot "Install-CortexGatewayService.ps1"
$logPath = Join-Path $runtimeDir "service-install.log"
$resultPath = Join-Path $runtimeDir "service-install-result.json"
$null = New-Item -ItemType Directory -Path $runtimeDir -Force

try {
    $output = & $installer -StartupTimeoutSeconds $StartupTimeoutSeconds 2>&1 | Out-String
    [IO.File]::WriteAllText($logPath, $output, (New-Object Text.UTF8Encoding($false)))
    [ordered]@{
        success = $true
        completedAt = (Get-Date).ToUniversalTime().ToString("o")
        logPath = $logPath
    } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 0
}
catch {
    $details = $_.Exception.ToString() + [Environment]::NewLine + ($_ | Out-String)
    [IO.File]::WriteAllText($logPath, $details, (New-Object Text.UTF8Encoding($false)))
    [ordered]@{
        success = $false
        completedAt = (Get-Date).ToUniversalTime().ToString("o")
        error = $_.Exception.Message
        logPath = $logPath
    } | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
